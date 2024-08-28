# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# Quantization API library for torchchat.
#
# NOTICE: most of the quant primitives code here will be deprecated in favor of torchao quantization APIs.
#
# Here are the quantization APIs available:
#   * quantize_model(): the entry point for all quantization with different options.
#   * QuantHandler: a base class for quantization handlers. This will be deprecated in favorr of torchao API.
#
# Different implementation of Handlers:
#   * EmbeddingOnlyQuantHandler: quantize embeddings.
#   * WeightOnlyInt8QuantHandler: int8 weight only quantization. Will be migrated to torchao API.
#   * WeightOnlyInt4QuantHandler: int4 weight only quantization. Will be migrated to torchao API.
#
# torchao Quantizer:
#   * Int8DynActInt4WeightQuantizer: dynamic quantization for int8 acitvation and int4 weight. Using torchao API.
#
from __future__ import annotations

import json

# from functools import reduce
# from math import gcd
from typing import Dict, Optional

import torch
import torch.nn as nn
import torch.nn.functional as F
from build.utils import get_device_str, get_precision, name_to_dtype, state_dict_device

from quantization.qops import LinearInt8 as WeightOnlyInt8Linear, QuantizedEmbedding

# AttributeError: '_OpNamespace' 'quantized_decomposed' object has no attribute 'quantize_per_channel_group'
from torch.ao.quantization.fx._decomposed import quantized_decomposed_lib  # noqa
from torchao.quantization.quant_api import (
    int4_weight_only,
    Int4WeightOnlyQuantizer,
    Int8DynActInt4WeightQuantizer,
    quantize_,
)
from torchao.utils import unwrap_tensor_subclass


#########################################################################
###                  torchchat quantization API                       ###


def quantize_model(
    model: nn.Module,
    device,
    quantize_options,
    tokenizer=None,
    support_tensor_subclass: bool = True,
):
    """
    Quantize the specified model using the quantizers described by
    a quantization dict of the form:
    {
        'embedding':   {'bitwidth': 8, 'groupsize': 8},
        'linear:int8': {'bitwidth': 8, 'groupsize': 8},
        'precision':   {'dtype': torch.float16},
    }
    """

    if isinstance(quantize_options, str):
        quantize_options = json.loads(quantize_options)

    for quantizer, q_kwargs in quantize_options.items():
        if (
            quantizer not in quantizer_class_dict
            and quantizer not in ao_quantizer_class_dict
        ):
            raise RuntimeError(f"unknown quantizer {quantizer} specified")
        if quantizer in ao_quantizer_class_dict:
            # Use tensor subclass API for int4 weight only.
            if device == "cuda" and quantizer == "linear:int4":
                quantize_(model, int4_weight_only(q_kwargs["groupsize"]))
                if not support_tensor_subclass:
                    unwrap_tensor_subclass(model)
                continue
            # Use dtype precision specified in user config, else fallback on global precision.
            if "precision" in quantize_options:
                dtype = quantize_options["precision"].get("dtype", str(get_precision()))
                precision = name_to_dtype(dtype, device)
            else:
                precision = get_precision()

            try:
                # Easier to ask forgiveness than permission
                quant_handler = ao_quantizer_class_dict[quantizer](
                    groupsize=q_kwargs["groupsize"], device=device, precision=precision
                )
            except TypeError as e:
                if "unexpected keyword argument 'device'" in str(e):
                    quant_handler = ao_quantizer_class_dict[quantizer](
                        groupsize=q_kwargs["groupsize"], precision=precision
                    )
                elif "unexpected keyword argument 'precision'" in str(e):
                    quant_handler = ao_quantizer_class_dict[quantizer](
                        groupsize=q_kwargs["groupsize"], device=device
                    )
                else:
                    raise e
            model = quant_handler.quantize(model)
        else:
            model = quantizer_class_dict[quantizer](
                model, device=device, tokenizer=tokenizer, **q_kwargs
            ).quantized_model()


#########################################################################
###                QuantHandler API definition                        ###
###               (unify with torchao in future)                      ###


class QuantHandler:
    def __init__(self, model: nn.Module, device="cpu", tokenizer=None):
        self.model_ = model
        self.device = device
        self.tokenizer = tokenizer

    def create_quantized_state_dict(self) -> Dict:  # "StateDict"
        pass

    def convert_for_runtime(self) -> nn.Module:
        pass

    def quantized_model(self) -> nn.Module:
        model_updated_state_dict = state_dict_device(self.create_quantized_state_dict())
        self.convert_for_runtime()
        self.model_.load_state_dict(model_updated_state_dict)
        return self.model_


#########################################################################
###           wrapper for setting precision as a QuantHandler         ###


class PrecisionHandler(QuantHandler):
    def __init__(self, model: nn.Module, device="cpu", tokenizer=None, *, dtype):
        self.model_ = model
        self.device = device
        self.tokenizer = tokenizer

        if isinstance(dtype, str):
            dtype = name_to_dtype(dtype, device)
        self.dtype = dtype

    def create_quantized_state_dict(self) -> Dict:  # "StateDict"
        pass

    def convert_for_runtime(self) -> nn.Module:
        pass

    def quantized_model(self) -> nn.Module:
        return self.model_.to(device=self.device, dtype=self.dtype)


#########################################################################
###            wrapper for setting device as a QuantHandler           ###
###    for onw select device for PyTorch eager and AOTI, in future    ###
###    also use this for selecting delegate when exporting with ET    ###


class ExecutorHandler(QuantHandler):
    def __init__(self, model: nn.Module, device="cpu", tokenizer=None, *, accelerator):
        self.model_ = model

        if isinstance(accelerator, str):
            device = get_device_str(accelerator)
        self.device = device

    def create_quantized_state_dict(self) -> Dict:  # "StateDict"
        pass

    def convert_for_runtime(self) -> nn.Module:
        pass

    def quantized_model(self) -> nn.Module:
        return self.model_.to(device=self.device)


#########################################################################
#####                     Quantization Primitives                  ######


def dynamically_quantize_per_channel(
    x,
    quant_min,
    quant_max,
    target_dtype,
    groupsize: Optional[int] = None,
    *,
    scales_dtype=torch.float16,
    enable_non_multiple_groups=True,
):
    """
    Dynamically quantize per channel.  This function is used for quantizing weights,
    for linear and embedding layers.

    Arguments:
        x: input tensor,
        quant_min: minimum value after quantization,
        quant_max: maximum value after quantization,
        target_dtype: target data type for weights after quantization,
        groupsize: number of elements of the channel to quantize together

    Keyword arguments:
        scales_dtype: data type of scale,
        enable_non_multiple_groups: if True, allow the rowsize to not be a multiple of group size,
                        with a final group of a size less than group size.

    Assumptions:
        This function assumes symmetric quantization, axis ==0 and a dense memory format.
    """

    # assumes symmetric quantization
    # assumes axis == 0
    # assumes dense memory format
    # TODO(future): relax ^ as needed

    x_shape_1 = x.shape[1]

    if groupsize is None or groupsize == 0:
        items = x_shape_1
    elif ((x_shape_1 % groupsize) == 0) or not enable_non_multiple_groups:
        assert groupsize > 0, "group size must be positive"
        assert (
            x_shape_1 % groupsize
        ) == 0, f"weights dimension 1 = {x_shape_1} must be a multiple of group size {groupsize}"
        items = groupsize
    else:
        assert groupsize > 0, "group size must be positive"
        print(
            f"row-size of weight matrix {x_shape_1} is not divisible by group size {groupsize}, using nearest neighbor rounding"
        )
        assert (
            x_shape_1 % groupsize != 0
        ), f"expected x.shape[1] to not be a multiple of group size {groupsize}, but got {x_shape_1}"
        padding = groupsize - (x_shape_1 % groupsize)
        x = F.pad(x, (0, padding))
        items = groupsize

    # default setup for affine quantization of activations
    eps = torch.finfo(torch.float32).eps

    x = x.view(x.shape[0], x.shape[1] // items, items)
    # get min and max
    min_val, max_val = torch.aminmax(x, dim=2)
    # print(f"min_val {min_val}")
    # print(f"max_val {max_val}")

    # calculate scales and zero_points based on min and max
    # reference: https://fburl.com/code/srbiybme
    min_val_neg = torch.min(min_val, torch.zeros_like(min_val))
    max_val_pos = torch.max(max_val, torch.zeros_like(max_val))
    device = min_val_neg.device

    # reference: https://fburl.com/code/4wll53rk
    max_val_pos = torch.max(-min_val_neg, max_val_pos)
    scales = max_val_pos / (float(quant_max - quant_min) / 2)
    # ensure scales is the same dtype as the original tensor
    scales = torch.clamp(scales, min=eps).to(x.dtype)
    zero_points = torch.zeros(min_val_neg.size(), dtype=torch.int64, device=device)

    # quantize based on qmin/qmax/scales/zp
    # reference: https://www.internalfb.com/code/fbsource/[8edc275012b1]/fbcode/caffe2/torch/ao/quantization/fx/_decomposed.py?lines=63
    x_div = x / scales.unsqueeze(-1)
    x_round = torch.round(x_div)
    x_zp = x_round + zero_points.unsqueeze(-1)
    quant = (
        torch.clamp(x_zp, quant_min, quant_max).to(target_dtype).view(x.shape[0], -1)
    )

    scales = scales.to(dtype=scales_dtype)
    quant = quant[:, :x_shape_1]

    return quant, scales, zero_points


def get_group_qparams(w, n_bit=4, groupsize=128, *, scales_dtype=torch.float):
    # needed for GPTQ with padding
    if groupsize > w.shape[-1]:
        groupsize = w.shape[-1]
    assert groupsize > 1
    assert w.shape[-1] % groupsize == 0
    assert w.dim() == 2

    to_quant = w.reshape(-1, groupsize)
    assert torch.isnan(to_quant).sum() == 0

    max_val = to_quant.amax(dim=1, keepdim=True)
    min_val = to_quant.amin(dim=1, keepdim=True)
    max_int = 2**n_bit - 1
    scales = (max_val - min_val).clamp(min=1e-6) / max_int
    zeros = min_val + scales * (2 ** (n_bit - 1))
    return scales.to(scales_dtype).reshape(w.shape[0], -1), zeros.to(
        scales_dtype
    ).reshape(w.shape[0], -1)


def pack_scales_and_zeros(scales, zeros, *, scales_dtype=torch.float):
    assert scales.shape == zeros.shape
    assert scales.dtype == scales_dtype
    assert zeros.dtype == scales_dtype
    return (
        torch.cat(
            [
                scales.reshape(scales.size(0), scales.size(1), 1),
                zeros.reshape(zeros.size(0), zeros.size(1), 1),
            ],
            2,
        )
        .transpose(0, 1)
        .contiguous()
    )


def unpack_scales_and_zeros(scales_and_zeros):
    assert len(scales_and_zeros.shape) == 3 and scales_and_zeros.shape[2] == 2
    assert scales_and_zeros.dtype == torch.float
    return torch.split(scales_and_zeros.transpose(0, 1), 1, 2)


def group_quantize_tensor_from_qparams(w, scales, zeros, n_bit=4, groupsize=128):
    assert groupsize > 1
    # needed for GPTQ single column quantize
    if groupsize > w.shape[-1] and scales.shape[-1] == 1:
        groupsize = w.shape[-1]

    assert w.shape[-1] % groupsize == 0
    assert w.dim() == 2

    to_quant = w.reshape(-1, groupsize)
    assert torch.isnan(to_quant).sum() == 0

    scales = scales.reshape(-1, 1)
    zeros = zeros.reshape(-1, 1)
    min_val = zeros - scales * (2 ** (n_bit - 1))
    max_int = 2**n_bit - 1
    min_int = 0
    w_int32 = (
        to_quant.sub(min_val)
        .div(scales)
        .round()
        .clamp_(min_int, max_int)
        .to(torch.int32)
        .reshape_as(w)
    )

    return w_int32


def group_quantize_tensor(w, n_bit=4, groupsize=128):
    scales, zeros = get_group_qparams(w, n_bit, groupsize)
    w_int32 = group_quantize_tensor_from_qparams(w, scales, zeros, n_bit, groupsize)
    scales_and_zeros = pack_scales_and_zeros(scales, zeros)
    return w_int32, scales_and_zeros


def group_dequantize_tensor_from_qparams(
    w_int32, scales, zeros, n_bit=4, groupsize=128
):
    assert groupsize > 1
    # needed for GPTQ single column dequantize
    if groupsize > w_int32.shape[-1] and scales.shape[-1] == 1:
        groupsize = w_int32.shape[-1]
    assert w_int32.shape[-1] % groupsize == 0
    assert w_int32.dim() == 2

    w_int32_grouped = w_int32.reshape(-1, groupsize)
    scales = scales.reshape(-1, 1)
    zeros = zeros.reshape(-1, 1)

    w_dq = (
        w_int32_grouped.sub(2 ** (n_bit - 1)).mul(scales).add(zeros).reshape_as(w_int32)
    )
    return w_dq


def group_dequantize_tensor(w_int32, scales_and_zeros, n_bit=4, groupsize=128):
    scales, zeros = unpack_scales_and_zeros(scales_and_zeros)
    return group_dequantize_tensor_from_qparams(
        w_int32, scales, zeros, n_bit, groupsize
    )


#########################################################################
#####          Weight-only int8 per-channel quantized code         ######


class WeightOnlyInt8QuantHandler(QuantHandler):
    def __init__(
        self,
        model: nn.Module,
        device,
        tokenizer=None,
        *,
        node_type: str = "*",
        bitwidth: Optional[int] = None,
        groupsize: Optional[int] = None,
    ):
        self.model_ = model
        self.device = device
        self.groupsize = groupsize
        self.node_type = node_type
        if bitwidth is None:
            self.bitwidth = 8
        else:
            self.bitwidth = bitwidth

    @torch.no_grad()
    def quantize(self, module):
        # cur_state_dict = state_dict_device(self.model_.state_dict())
        # dict_device = "cpu"  # self.device

        if self.bitwidth == 4:
            range_min = -8
            range_max = 7
        elif self.bitwidth == 8:
            range_min = -128
            range_max = 127
        else:
            raise ValueError(f"Unsupported bitwidth {self.bitwidth}")

        for name, child in module.named_children():
            # print(f"name: {name}")
            if isinstance(child, nn.Linear):
                if (
                    (self.node_type == "*")
                    or (self.node_type == "output" and name == "output")
                    or (self.node_type == "!output" and name != "output")
                ):
                    # print(f"{name, child}")
                    input_weight = child.weight.float()
                    # print(f"{name, child}")
                    # print(f"in_features: {child.in_features}")
                    # print(f"out_features: {child.out_features}")

                    # print(f"expanded weight shape {input_weight.shape}")
                    weight, scales, _ = dynamically_quantize_per_channel(
                        input_weight,
                        range_min,
                        range_max,
                        torch.int8,
                        self.groupsize,
                        scales_dtype=child.weight.dtype,
                    )

                    setattr(
                        module,
                        name,
                        WeightOnlyInt8Linear(
                            in_features=child.in_features,
                            out_features=child.out_features,
                            device=self.device,
                            # update variables from quantization
                            weight=weight,
                            scales=scales,
                            groupsize=self.groupsize,
                        ),
                    )
            else:
                self.quantize(child)

        return module

    def quantized_model(self) -> nn.Module:
        return self.quantize(self.model_)


#########################################################################
#####                   embedding table quantization               ######


class EmbeddingOnlyQuantHandler(QuantHandler):
    def __init__(
        self,
        model: nn.Module,
        device,
        tokenizer=None,
        *,
        bitwidth: int = 8,
        groupsize: Optional[int] = None,
        packed=True,  # we always pack bitwidth 4 now
    ):
        self.model_ = model
        self.device = device
        self.groupsize = groupsize
        self.bitwidth = bitwidth

    @torch.no_grad()
    def quantize(self, module):
        if self.bitwidth == 4:
            range_min = -8
            range_max = 7
        elif self.bitwidth == 8:
            range_min = -128
            range_max = 127
        else:
            raise ValueError(f"Unsupported bitwidth {self.bitwidth}")

        for name, child in module.named_children():
            # print(f"name: {name}")
            if isinstance(child, nn.Embedding):
                # print(f"Embedding identified: {fqn, mod}")
                # print(f"weights size: {child.weight.size()}")
                # print(f"quantize {fqn}...")

                # print(
                #     f"quantize {fqn, mod} with groupsize {self.groupsize}, bitwidth {self.bitwidth}"
                # )
                weight, scales, _ = dynamically_quantize_per_channel(
                    child.weight.float(),
                    range_min,
                    range_max,
                    torch.int8,
                    self.groupsize,
                    scales_dtype=child.weight.dtype,
                )

                if self.bitwidth == 4:
                    if weight.shape[-1] % 2 != 0:
                        raise RuntimeError("automatic padding not implemented yet")

                    weight_range_shifted = weight.add(8).view(torch.uint8)
                    weight_view = weight_range_shifted.view(
                        weight.shape[0], weight.shape[1] // 2, 2
                    )
                    weight_even = weight_view[:, :, 0] * 16  # left shift 4
                    weight_odd = weight_view[:, :, 1]
                    weight_packed = weight_even + weight_odd
                    weight = weight_packed

                weight = weight
                scales = scales.squeeze(dim=-1)

                # print(f"{name, child}")
                # print(f"weights size: {child.weight.size()}")
                setattr(
                    module,
                    name,
                    QuantizedEmbedding(
                        num_embeddings=child.weight.shape[0],
                        embedding_dim=child.weight.shape[1],
                        device=self.device,
                        bitwidth=self.bitwidth,
                        groupsize=self.groupsize,
                        weight=weight,
                        scales=scales,
                    ),
                )
            else:
                self.quantize(child)

        return module

    def quantized_model(self) -> nn.Module:
        return self.quantize(self.model_)


class CustomHandler(QuantHandler):
    def __init__(self, model: nn.Module, device="cpu", tokenizer=None):
        self.model_ = model
        self.device = device
        self.tokenizer = tokenizer

    def create_quantized_state_dict(self) -> Dict:  # "StateDict"
        pass

    def convert_for_runtime(self) -> nn.Module:
        pass

    def quantized_model(self) -> nn.Module:
        self.model_ = self.model_.to("cpu")

        import importlib.util
        import sys
        spec = importlib.util.spec_from_file_location(
            "torch_custom_op",
            "/Users/scroy/fbsource/fbcode/pytorch/ao/torchao/experimental/kernels/cpu/linear/examples/torch_custom_op/torch_custom_op.py"
        )
        torch_custom_op = importlib.util.module_from_spec(spec)
        sys.modules["torch_custom_op"] = torch_custom_op
        spec.loader.exec_module(torch_custom_op)

        torch_custom_op.replace_linear_with_quantized_linear(self.model_, kwargs={"group_size": 256, "nbit": 4, "squeeze_unsqueeze_dim0": True})
        return self.model_

##########################################################################
###                       quantization dictionary                      ###

# Map each quantizer configuration to a class implementing that quantizer
# Must come last because __future__ annotations don't work for naked
# class references
quantizer_class_dict = {
    "embedding": EmbeddingOnlyQuantHandler,
    "linear:int8": WeightOnlyInt8QuantHandler,
    "precision": PrecisionHandler,
    "executor": ExecutorHandler,
    "_custom": CustomHandler,
}

ao_quantizer_class_dict = {
    "linear:int4": Int4WeightOnlyQuantizer,
    "linear:a8w4dq": Int8DynActInt4WeightQuantizer,
}
