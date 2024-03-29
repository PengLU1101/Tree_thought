#!/usr/bin/env python
# -*- coding: utf-8 -*-
# author：Peng time:2019-07-16
from typing import List, Tuple, Optional, Dict, Union, Callable
from collections import namedtuple
import random

import torch
import torch.nn as nn
from torch.nn.utils.rnn import pack_padded_sequence as pack
from torch.nn.utils.rnn import pad_packed_sequence as unpack
import torch.nn.functional as F
from torch import Tensor as T
import numpy as np

from NNLayers.Embeddings import Embedding_Net, WordEmbedding, PositionalEncoding
from NNLayers.Gate_Net import Gate_Net, Score_Net
from NNLayers.Predict_Net import Predic_Net


class TransformerEncoder(nn.Module):
    def __init__(self,
                 vocab,
                 emb_dim,
                 d_model,
                 nhead,
                 n_layer,
                 dropout):
        super(TransformerEncoder, self).__init__()

        # word emb layer
        self.wordemb = WordEmbedding(vocab, emb_dim)
        # postition emb layer
        self.positionemb = PositionalEncoding(dropout, emb_dim)
        self.enc_layer = nn.TransformerEncoder(
            nn.TransformerEncoderLayer(d_model=d_model,
                                       nhead=nhead,
                                       dropout=dropout),
            num_layers=n_layer,
            norm=nn.LayerNorm(d_model)
        )

        self.ffn = nn.Sequential(
            nn.Linear(d_model, d_model),
            nn.ReLU(),
            nn.Linear(d_model, d_model),
            nn.LayerNorm(d_model)
        )
        self.emb_dim = emb_dim
        self.d_model = d_model

        if emb_dim != d_model:
            self.prejector = nn.Sequential(
            nn.Linear(emb_dim, d_model),
            nn.ReLU(),
        )

    def forward(self,
                src: T,
                mask: T,
                idx_list: Optional[List[List[int]]] = None,
                length: Optional[List[str]] = None) -> Union[List[T], T]:
        rep = self.positionemb(self.wordemb(src)).permute(1, 0, 2)
        if self.emb_dim != self.d_model:
            rep = self.prejector(rep)
        rep = self.ffn(self.enc_layer(
            src=rep,
            src_key_padding_mask=mask.eq(0))).permute(1, 0, 2)[:, 0, :]
        if idx_list:
            rep = [torch.index_select(rep,
                                      dim=0,
                                      index=torch.LongTensor(idx).to(src.device)) for idx in idx_list]

        return rep


class LSTMEncoder(nn.Module):
    def __init__(self,
                 vocab,
                 emb_dim,
                 d_model,
                 n_layer,
                 dropout,
                 bidirectional):
        super(LSTMEncoder, self).__init__()
        self.wordemb = WordEmbedding(vocab, emb_dim)
        self.rnn = nn.LSTM(
            emb_dim,
            d_model,
            n_layer,
            bidirectional=bidirectional
        )
        self.Dropout = nn.Dropout(dropout)
        self.bidirectional = bidirectional

    def forward(self,
                input: T,
                mask: T,
                idx_list: Optional[List[List[int]]] = None,
                lengths: List[int] = None) -> Union[List[T], T]:
        input = self.Dropout(self.wordemb(input).permute(1, 0, 2))
        packed_seq = pack(
            input,
            lengths,
            enforce_sorted=False
        )
        out, (h, c) = self.rnn(packed_seq)
        # print(f'h size: {h.size()}')
        # print(f'c size: {c.size()}')
        if self.bidirectional:
            h = torch.cat((h[-1], h[-2]), dim=-1)

        else:
            h = h[-1]
        if idx_list:
            h = [torch.index_select(h,
                                    dim=0,
                                    index=torch.LongTensor(idx).to(input.device)) for idx in idx_list]

        return h

class Parser(nn.Module):
    def __init__(self,
                 d_model,
                 dropout,
                 score_type,
                 resolution,
                 hard):
        super(Parser, self).__init__()
        self.score_layer = Score_Net(
            d_model,
            dropout,
            score_type
        )
        self.gate_layer = Gate_Net(
            d_model,
            dropout,
            resolution,
            hard
        )
    def forward(self,
                rep_srcs: List[T],
                rep_idx: List[List[int]],
                score_idx: List[List[int]]) -> List[Tuple[T, T]]:
        scores = self.score_layer(rep_srcs)
        return self.gate_layer(scores, rep_srcs, rep_idx, score_idx)


class PEmodel(nn.Module):
    def __init__(self,
                 encoder,
                 parser,
                 predictor,
                 loss_func=None):
        super(PEmodel, self).__init__()
        self.encoder = encoder
        self.parser = parser
        self.predictor = predictor
        if loss_func:
            self.loss_func = loss_func
    def forward(self,
                input: T,
                mask: T,
                rep_idx: List[List[int]],
                score_idx: List[List[int]],
                neg_input: Tuple[T, T],
                neg_mask: Tuple[T, T],
                length_dict: Dict[str, List[int]],
                flag_quick: bool) -> Tuple[T, T, List[Tuple[T, T]]]:
        reps: List[T] = self.encoder(input, mask, rep_idx, length_dict['src'])
        neg_fwd: T = self.encoder(neg_input[0], neg_mask[0], None, length_dict['nf'])
        neg_bwd: T = self.encoder(neg_input[1], neg_mask[1], None, length_dict['nb'])
        gate_list: List[Tuple[T, T]] = self.parser(reps, rep_idx, score_idx)

        if self.predictor.score_type in ['denselinear', 'linear']:
            lld, mask = self.predictor(
                reps,
                gate_list,
                neg_fwd,
                neg_bwd,
                flag_quick
            )
            fwd_pos_label = torch.ones(
                lld['fwd_pos'].size(0),
                requires_grad=False
            ).to(neg_fwd.device).long()
            fwd_neg_label = torch.zeros(
                lld['fwd_neg'].size(0),
                requires_grad=False
            ).to(neg_fwd.device).long()
            loss_pos = self.loss_func(lld['fwd_pos'].squeeze(1), fwd_pos_label)
            loss_neg = self.loss_func(lld['fwd_neg'].squeeze(1), fwd_neg_label)

            if self.predictor.bidirectional:
                bwd_pos_label = torch.ones(
                    lld['bwd_pos'].size(0),
                    requires_grad=False
                ).to(neg_fwd.device).long()
                bwd_neg_label = torch.zeros(
                    lld['bwd_neg'].size(0),
                    requires_grad=False
                ).to(neg_fwd.device).long()
                loss_pos = (loss_pos + self.loss_func(lld['bwd_pos'].squeeze(1), bwd_pos_label)) / 2
                loss_neg = (loss_neg + self.loss_func(lld['bwd_neg'].squeeze(1), bwd_neg_label)) / 2

        else:
            lld, mask = self.predictor(
                reps,
                gate_list,
                neg_fwd,
                neg_bwd,
                flag_quick
            )
            loss_pos = -torch.mean(lld['fwd_pos'])
            loss_neg = -torch.mean(lld['fwd_neg'])
            if self.predictor.bidirectional:
                loss_pos = (loss_pos - torch.mean(lld['bwd_pos'])) / 2
                loss_neg = (loss_neg - torch.mean(lld['bwd_neg'])) / 2
        return (loss_pos, loss_neg, mask)

    @staticmethod
    def encode(model,
               input: T,
               mask: T,
               length_dict: Dict[str, List[int]]) -> T:
        model.eval()
        reps: T = model.encoder(input, mask, None, length_dict['src'])
        return reps

    @staticmethod
    def train_step(model,
                   optimizer,
                   scheduler,
                   data,
                   args,
                   istep):
        model.train()
        optimizer.zero_grad()
        flag_quick = True
        Tensor_dict, idx_dict, length_dict = data
        if istep > args.quick_thought_step:
            flag_quick = False

        pos_loss, neg_loss, gate_list = model(
            Tensor_dict['src'].cuda(),
            Tensor_dict['mask_src'].cuda(),
            idx_dict['rep_idx'],
            idx_dict['score_idx'],
            (Tensor_dict['nf'].cuda(), Tensor_dict['nb'].cuda()),
            (Tensor_dict['mnf'].cuda(), Tensor_dict['mnb'].cuda()),
            length_dict,
            flag_quick
        )
        loss = (pos_loss + neg_loss) / 2
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_grad_norm)
        optimizer.step()
        scheduler.step()
        # if istep == 1000:
        #     for x in gate_list:
        #         print(f"gate is: \n{x[0]}")

        log = {
            #**regularization_log,
            'positive_sample_loss': pos_loss.item(),
            'negative_sample_loss': neg_loss.item(),
            'loss': loss.item()
        }

        return log

    @staticmethod
    def test_step(model,
                  data,
                  args):
        model.eval()
        Tensor_dict, token_dict, idx_dict, length_dict = data
        pos_loss, neg_loss, gate_list = model(
            Tensor_dict['src'].cuda(),
            Tensor_dict['mask_src'].cuda(),
            idx_dict['rep_idx'],
            idx_dict['score_idx'],
            (Tensor_dict['nf'].cuda(), Tensor_dict['nb'].cuda()),
            (Tensor_dict['mnf'].cuda(), Tensor_dict['mnb'].cuda()),
            length_dict,
            #idx_dict['neg_idx']
        )
        loss = (pos_loss + neg_loss) / 2

        log = {
            #**regularization_log,
            'positive_sample_loss': pos_loss.item(),
            'negative_sample_loss': neg_loss.item(),
            'loss': loss.item()
        }

        return log

def build_model(para, weight=None):
    if para.encoder_type == 'transformer':
        encoder = TransformerEncoder(
            para.word2id,
            para.emb_dim,
            para.d_model,
            para.nhead,
            para.n_layer,
            para.dropout
        )
    elif para.encoder_type == 'LSTM':
        if para.bidirectional:
            d_model = para.d_model // 2
        else:
            d_model = para.d_model
        encoder = LSTMEncoder(
            para.word2id,
            para.emb_dim,
            d_model,
            para.n_layer,
            para.dropout,
            para.bidirectional

        )

    encoder.wordemb.apply_weights(weight)

    parser = Parser(
        para.d_model,
        para.dropout,
        para.score_type_parser,
        para.resolution,
        para.hard
    )

    predictor = Predic_Net(
        para.d_model,
        para.score_type_predictor,
        para.bidirectional_compute
    )
    if para.score_type_predictor in ['denselinear', 'linear']:
        loss_func = nn.NLLLoss()
    else:
        loss_func = None
    return PEmodel(encoder, parser, predictor, loss_func)

def get_idx_by_lens(lens_list: List[int]) -> List[List[int]]:
    idx_list: List[List[int]] = []
    start = 0
    for i in range(len(lens_list)):
        idx_list += [list(range(start, start + lens_list[i]))]
        start = idx_list[-1][-1] + 1
    return idx_list

def get_neglist(lens_list):
    neg = []
    for x in lens_list:
        neg.append((smpneg(list(range(x))), smpneg(list(range(x))[::-1])))
    return neg

def smpneg(l):
    _ = []
    ll = l + l
    for i in range(1, len(l)):
        _ += [random.choice(ll[i + 1: i + 6])]
    return _

def test():
    word_emb = WordEmbedding(100, 20)
    position_emb = PositionalEncoding(0.5, 20)
    emb_layer = Embedding_Net(
        word_emb,
        position_emb,
    )
    enc_layer = nn.TransformerEncoder(
        nn.TransformerEncoderLayer(d_model=20, nhead=2, dropout=0.5),
        num_layers=3,
        norm=nn.LayerNorm(20)
    )
    encoder = Encoder(emb_layer, enc_layer)

    score_layer = Score_Net(
        20,
        0.5,
        'dot'
    )
    gate_layer = Gate_Net(
        20,
        0.5,
        1,
        'dot'
    )
    parser = Parser(score_layer, gate_layer)

    predictor = Predic_Net(
        20,
        'dot'
    )
    model = PEmodel(encoder, parser, predictor)

    data = list(range(100))
    random.shuffle(data)
    t = torch.LongTensor(data).view(25, 4)
    mask = torch.ones(25, 4).long()
    #mask[0, :] = 0
    len_list = [4, 5, 10, 6]
    repidx = get_idx_by_lens(len_list)
    scoreidx = get_idx_by_lens([x+1 for x in len_list])
    neg_idx = get_neglist(len_list)

    lp, ln = model(t, mask, repidx, scoreidx, neg_idx)
    print(lp)
    print(ln)

if __name__ == "__main__":
    test()