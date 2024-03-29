#!/usr/bin/env python
# -*- coding: utf-8 -*-
# author：Peng time:2019-09-16

from typing import List, Tuple, Dict, Callable, Iterator

import torch
import torch.nn as nn
from torch import Tensor as T

from transformer.Layers import EncoderLayer


class TransformerEncoder(nn.Module):

    def __init__(self,
                 input_size: int,
                 hidden_size: int,
                 n_layer: int,
                 decoder: str,
                 ff_size: int = 2048,
                 n_head: int = 8,
                 dropout: float = 0.1):

        super(TransformerEncoder, self).__init__()

        self.input_size = input_size
        self.hidden_size = hidden_size
        self.d_inner = ff_size
        self.n_head = n_head
        self.dropout = dropout
        self.decoder = decoder
        if decoder == 'SL':
            self.n_head = 16
            self.dropout = 0.2
        self.n_layer = n_layer
        self.d_k = self.d_v = int(self.hidden_size / self.n_head)

        self.layer_stack = nn.ModuleList([
            EncoderLayer(
                self.hidden_size,
                self.d_inner,
                self.n_head,
                self.d_k,
                self.d_v,
                dropout=self.dropout)
            for _ in range(self.n_layer)])

    def forward(self,
                inputs: T,
                non_pad_mask: T,
                attn_mask: T) -> T:

        bs, seq_len = inputs.size(0), inputs.size(1)

        assert inputs.size() == (bs, seq_len, self.hidden_size)
        assert non_pad_mask.size() == (bs, seq_len, 1)
        assert attn_mask.size() == (bs, seq_len, seq_len)

        output = []
        for layer in self.layer_stack:
            inputs, enc_slf_atten = layer(
                inputs,
                non_pad_mask=non_pad_mask,
                slf_attn_mask=attn_mask
            )
            output.append(inputs)
        if self.decoder == 'SL':
            output = output[-1] + output[-2] + output[-3] + output[-4]
        else:
            output = output[-1]

        assert output.size() == (bs, seq_len, self.hidden_size)

        return output

