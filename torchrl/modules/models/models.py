# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
from __future__ import annotations

import dataclasses

import warnings
from numbers import Number
from typing import Dict, List, Optional, Sequence, Tuple, Type, Union

import torch
from tensordict.nn import dispatch, TensorDictModuleBase
from torch import nn
from torch.nn import functional as F

from torchrl._utils import prod
from torchrl.data.utils import DEVICE_TYPING
from torchrl.modules.models.decision_transformer import DecisionTransformer
from torchrl.modules.models.utils import (
    _find_depth,
    create_on_device,
    LazyMapping,
    SquashDims,
    Squeeze2dLayer,
    SqueezeLayer,
)


class MLP(nn.Sequential):
    """A multi-layer perceptron.

    If MLP receives more than one input, it concatenates them all along the last dimension before passing the
    resulting tensor through the network. This is aimed at allowing for a seamless interface with calls of the type of

        >>> model(state, action)  # compute state-action value

    In the future, this feature may be moved to the ProbabilisticTDModule, though it would require it to handle
    different cases (vectors, images, ...)

    Args:
        in_features (int, optional): number of input features;
        out_features (int, list of int): number of output features. If iterable of integers, the output is reshaped to
            the desired shape;
        depth (int, optional): depth of the network. A depth of 0 will produce a single linear layer network with the
            desired input and output size. A length of 1 will create 2 linear layers etc. If no depth is indicated,
            the depth information should be contained in the num_cells argument (see below). If num_cells is an
            iterable and depth is indicated, both should match: len(num_cells) must be equal to depth.
        num_cells (int or Sequence[int], optional): number of cells of every layer in between the input and output. If
            an integer is provided, every layer will have the same number of cells. If an iterable is provided,
            the linear layers out_features will match the content of num_cells.
            default: 32;
        activation_class (Type[nn.Module]): activation class to be used.
            default: nn.Tanh
        activation_kwargs (dict, optional): kwargs to be used with the activation class;
        norm_class (Type, optional): normalization class, if any.
        norm_kwargs (dict, optional): kwargs to be used with the normalization layers;
        dropout (float, optional): dropout probability. Defaults to ``None`` (no
            dropout);
        bias_last_layer (bool): if ``True``, the last Linear layer will have a bias parameter.
            default: True;
        single_bias_last_layer (bool): if ``True``, the last dimension of the bias of the last layer will be a singleton
            dimension.
            default: True;
        layer_class (Type[nn.Module]): class to be used for the linear layers;
        layer_kwargs (dict, optional): kwargs for the linear layers;
        activate_last_layer (bool): whether the MLP output should be activated. This is useful when the MLP output
            is used as the input for another module.
            default: False.
        device (Optional[DEVICE_TYPING]): device to create the module on.

    Examples:
        >>> # All of the following examples provide valid, working MLPs
        >>> mlp = MLP(in_features=3, out_features=6, depth=0) # MLP consisting of a single 3 x 6 linear layer
        >>> print(mlp)
        MLP(
          (0): Linear(in_features=3, out_features=6, bias=True)
        )
        >>> mlp = MLP(in_features=3, out_features=6, depth=4, num_cells=32)
        >>> print(mlp)
        MLP(
          (0): Linear(in_features=3, out_features=32, bias=True)
          (1): Tanh()
          (2): Linear(in_features=32, out_features=32, bias=True)
          (3): Tanh()
          (4): Linear(in_features=32, out_features=32, bias=True)
          (5): Tanh()
          (6): Linear(in_features=32, out_features=32, bias=True)
          (7): Tanh()
          (8): Linear(in_features=32, out_features=6, bias=True)
        )
        >>> mlp = MLP(out_features=6, depth=4, num_cells=32)  # LazyLinear for the first layer
        >>> print(mlp)
        MLP(
          (0): LazyLinear(in_features=0, out_features=32, bias=True)
          (1): Tanh()
          (2): Linear(in_features=32, out_features=32, bias=True)
          (3): Tanh()
          (4): Linear(in_features=32, out_features=32, bias=True)
          (5): Tanh()
          (6): Linear(in_features=32, out_features=32, bias=True)
          (7): Tanh()
          (8): Linear(in_features=32, out_features=6, bias=True)
        )
        >>> mlp = MLP(out_features=6, num_cells=[32, 33, 34, 35])  # defines the depth by the num_cells arg
        >>> print(mlp)
        MLP(
          (0): LazyLinear(in_features=0, out_features=32, bias=True)
          (1): Tanh()
          (2): Linear(in_features=32, out_features=33, bias=True)
          (3): Tanh()
          (4): Linear(in_features=33, out_features=34, bias=True)
          (5): Tanh()
          (6): Linear(in_features=34, out_features=35, bias=True)
          (7): Tanh()
          (8): Linear(in_features=35, out_features=6, bias=True)
        )
        >>> mlp = MLP(out_features=(6, 7), num_cells=[32, 33, 34, 35])  # returns a view of the output tensor with shape [*, 6, 7]
        >>> print(mlp)
        MLP(
          (0): LazyLinear(in_features=0, out_features=32, bias=True)
          (1): Tanh()
          (2): Linear(in_features=32, out_features=33, bias=True)
          (3): Tanh()
          (4): Linear(in_features=33, out_features=34, bias=True)
          (5): Tanh()
          (6): Linear(in_features=34, out_features=35, bias=True)
          (7): Tanh()
          (8): Linear(in_features=35, out_features=42, bias=True)
        )
        >>> from torchrl.modules import NoisyLinear
        >>> mlp = MLP(out_features=(6, 7), num_cells=[32, 33, 34, 35], layer_class=NoisyLinear)  # uses NoisyLinear layers
        >>> print(mlp)
        MLP(
          (0): NoisyLazyLinear(in_features=0, out_features=32, bias=False)
          (1): Tanh()
          (2): NoisyLinear(in_features=32, out_features=33, bias=True)
          (3): Tanh()
          (4): NoisyLinear(in_features=33, out_features=34, bias=True)
          (5): Tanh()
          (6): NoisyLinear(in_features=34, out_features=35, bias=True)
          (7): Tanh()
          (8): NoisyLinear(in_features=35, out_features=42, bias=True)
        )

    """

    def __init__(
        self,
        in_features: Optional[int] = None,
        out_features: Union[int, Sequence[int]] = None,
        depth: Optional[int] = None,
        num_cells: Optional[Union[Sequence, int]] = None,
        activation_class: Type[nn.Module] = nn.Tanh,
        activation_kwargs: Optional[dict] = None,
        norm_class: Optional[Type[nn.Module]] = None,
        norm_kwargs: Optional[dict] = None,
        dropout: Optional[float] = None,
        bias_last_layer: bool = True,
        single_bias_last_layer: bool = False,
        layer_class: Type[nn.Module] = nn.Linear,
        layer_kwargs: Optional[dict] = None,
        activate_last_layer: bool = False,
        device: Optional[DEVICE_TYPING] = None,
    ):
        if out_features is None:
            raise ValueError("out_features must be specified for MLP.")

        default_num_cells = 32
        if num_cells is None:
            if depth is None:
                num_cells = [default_num_cells] * 3
                depth = 3
            else:
                num_cells = [default_num_cells] * depth

        self.in_features = in_features

        _out_features_num = out_features
        if not isinstance(out_features, Number):
            _out_features_num = prod(out_features)
        self.out_features = out_features
        self._out_features_num = _out_features_num
        self.activation_class = activation_class
        self.activation_kwargs = (
            activation_kwargs if activation_kwargs is not None else {}
        )
        self.norm_class = norm_class
        self.norm_kwargs = norm_kwargs if norm_kwargs is not None else {}
        self.dropout = dropout
        self.bias_last_layer = bias_last_layer
        self.single_bias_last_layer = single_bias_last_layer
        self.layer_class = layer_class
        self.layer_kwargs = layer_kwargs if layer_kwargs is not None else {}
        self.activate_last_layer = activate_last_layer
        if single_bias_last_layer:
            raise NotImplementedError

        if not (isinstance(num_cells, Sequence) or depth is not None):
            raise RuntimeError(
                "If num_cells is provided as an integer, \
            depth must be provided too."
            )
        self.num_cells = (
            list(num_cells) if isinstance(num_cells, Sequence) else [num_cells] * depth
        )
        self.depth = depth if depth is not None else len(self.num_cells)
        if not (len(self.num_cells) == depth or depth is None):
            raise RuntimeError(
                "depth and num_cells length conflict, \
            consider matching or specifying a constant num_cells argument together with a a desired depth"
            )
        layers = self._make_net(device)
        super().__init__(*layers)

    def _make_net(self, device: Optional[DEVICE_TYPING]) -> List[nn.Module]:
        layers = []
        in_features = [self.in_features] + self.num_cells
        out_features = self.num_cells + [self._out_features_num]
        for i, (_in, _out) in enumerate(zip(in_features, out_features)):
            _bias = self.bias_last_layer if i == self.depth else True
            if _in is not None:
                layers.append(
                    create_on_device(
                        self.layer_class,
                        device,
                        _in,
                        _out,
                        bias=_bias,
                        **self.layer_kwargs,
                    )
                )
            else:
                try:
                    lazy_version = LazyMapping[self.layer_class]
                except KeyError:
                    raise KeyError(
                        f"The lazy version of {self.layer_class.__name__} is not implemented yet. "
                        "Consider providing the input feature dimensions explicitely when creating an MLP module"
                    )
                layers.append(
                    create_on_device(
                        lazy_version, device, _out, bias=_bias, **self.layer_kwargs
                    )
                )

            if i < self.depth or self.activate_last_layer:
                if self.dropout is not None:
                    layers.append(create_on_device(nn.Dropout, device, p=self.dropout))
                if self.norm_class is not None:
                    layers.append(
                        create_on_device(self.norm_class, device, **self.norm_kwargs)
                    )
                layers.append(
                    create_on_device(
                        self.activation_class, device, **self.activation_kwargs
                    )
                )

        return layers

    def forward(self, *inputs: Tuple[torch.Tensor]) -> torch.Tensor:
        if len(inputs) > 1:
            inputs = (torch.cat([*inputs], -1),)

        out = super().forward(*inputs)
        if not isinstance(self.out_features, Number):
            out = out.view(*out.shape[:-1], *self.out_features)
        return out


class ConvNet(nn.Sequential):
    """A convolutional neural network.

    Args:
        in_features (int, optional): number of input features;
        depth (int, optional): depth of the network. A depth of 1 will produce a single linear layer network with the
            desired input size, and with an output size equal to the last element of the num_cells argument.
            If no depth is indicated, the depth information should be contained in the num_cells argument (see below).
            If num_cells is an iterable and depth is indicated, both should match: len(num_cells) must be equal to
            the depth.
        num_cells (int or Sequence[int], optional): number of cells of every layer in between the input and output. If
            an integer is provided, every layer will have the same number of cells. If an iterable is provided,
            the linear layers out_features will match the content of num_cells.
            default: [32, 32, 32];
        kernel_sizes (int, Sequence[Union[int, Sequence[int]]]): Kernel size(s) of the conv network. If iterable, the length must match the
            depth, defined by the num_cells or depth arguments.
        strides (int or Sequence[int]): Stride(s) of the conv network. If iterable, the length must match the
            depth, defined by the num_cells or depth arguments.
        activation_class (Type[nn.Module]): activation class to be used.
            default: nn.Tanh
        activation_kwargs (dict, optional): kwargs to be used with the activation class;
        norm_class (Type, optional): normalization class, if any;
        norm_kwargs (dict, optional): kwargs to be used with the normalization layers;
        bias_last_layer (bool): if ``True``, the last Linear layer will have a bias parameter.
            default: True;
        aggregator_class (Type[nn.Module]): aggregator to use at the end of the chain.
            default:  SquashDims;
        aggregator_kwargs (dict, optional): kwargs for the aggregator_class;
        squeeze_output (bool): whether the output should be squeezed of its singleton dimensions.
            default: False.
        device (Optional[DEVICE_TYPING]): device to create the module on.

    Examples:
        >>> # All of the following examples provide valid, working MLPs
        >>> cnet = ConvNet(in_features=3, depth=1, num_cells=[32,]) # MLP consisting of a single 3 x 6 linear layer
        >>> print(cnet)
        ConvNet(
          (0): Conv2d(3, 32, kernel_size=(3, 3), stride=(1, 1))
          (1): ELU(alpha=1.0)
          (2): SquashDims()
        )
        >>> cnet = ConvNet(in_features=3, depth=4, num_cells=32)
        >>> print(cnet)
        ConvNet(
          (0): Conv2d(3, 32, kernel_size=(3, 3), stride=(1, 1))
          (1): ELU(alpha=1.0)
          (2): Conv2d(32, 32, kernel_size=(3, 3), stride=(1, 1))
          (3): ELU(alpha=1.0)
          (4): Conv2d(32, 32, kernel_size=(3, 3), stride=(1, 1))
          (5): ELU(alpha=1.0)
          (6): Conv2d(32, 32, kernel_size=(3, 3), stride=(1, 1))
          (7): ELU(alpha=1.0)
          (8): SquashDims()
        )
        >>> cnet = ConvNet(in_features=3, num_cells=[32, 33, 34, 35])  # defines the depth by the num_cells arg
        >>> print(cnet)
        ConvNet(
          (0): Conv2d(3, 32, kernel_size=(3, 3), stride=(1, 1))
          (1): ELU(alpha=1.0)
          (2): Conv2d(32, 33, kernel_size=(3, 3), stride=(1, 1))
          (3): ELU(alpha=1.0)
          (4): Conv2d(33, 34, kernel_size=(3, 3), stride=(1, 1))
          (5): ELU(alpha=1.0)
          (6): Conv2d(34, 35, kernel_size=(3, 3), stride=(1, 1))
          (7): ELU(alpha=1.0)
          (8): SquashDims()
        )
        >>> cnet = ConvNet(in_features=3, num_cells=[32, 33, 34, 35], kernel_sizes=[3, 4, 5, (2, 3)])  # defines kernels, possibly rectangular
        >>> print(cnet)
        ConvNet(
          (0): Conv2d(3, 32, kernel_size=(3, 3), stride=(1, 1))
          (1): ELU(alpha=1.0)
          (2): Conv2d(32, 33, kernel_size=(4, 4), stride=(1, 1))
          (3): ELU(alpha=1.0)
          (4): Conv2d(33, 34, kernel_size=(5, 5), stride=(1, 1))
          (5): ELU(alpha=1.0)
          (6): Conv2d(34, 35, kernel_size=(2, 3), stride=(1, 1))
          (7): ELU(alpha=1.0)
          (8): SquashDims()
        )

    """

    def __init__(
        self,
        in_features: Optional[int] = None,
        depth: Optional[int] = None,
        num_cells: Union[Sequence, int] = None,
        kernel_sizes: Union[Sequence[Union[int, Sequence[int]]], int] = 3,
        strides: Union[Sequence, int] = 1,
        paddings: Union[Sequence, int] = 0,
        activation_class: Type[nn.Module] = nn.ELU,
        activation_kwargs: Optional[dict] = None,
        norm_class: Optional[Type[nn.Module]] = None,
        norm_kwargs: Optional[dict] = None,
        bias_last_layer: bool = True,
        aggregator_class: Optional[Type[nn.Module]] = SquashDims,
        aggregator_kwargs: Optional[dict] = None,
        squeeze_output: bool = False,
        device: Optional[DEVICE_TYPING] = None,
    ):
        if num_cells is None:
            num_cells = [32, 32, 32]

        self.in_features = in_features
        self.activation_class = activation_class
        self.activation_kwargs = (
            activation_kwargs if activation_kwargs is not None else {}
        )
        self.norm_class = norm_class
        self.norm_kwargs = norm_kwargs if norm_kwargs is not None else {}
        self.bias_last_layer = bias_last_layer
        self.aggregator_class = aggregator_class
        self.aggregator_kwargs = (
            aggregator_kwargs if aggregator_kwargs is not None else {"ndims_in": 3}
        )
        self.squeeze_output = squeeze_output
        # self.single_bias_last_layer = single_bias_last_layer

        depth = _find_depth(depth, num_cells, kernel_sizes, strides, paddings)
        self.depth = depth
        if depth == 0:
            raise ValueError("Null depth is not permitted with ConvNet.")

        for _field, _value in zip(
            ["num_cells", "kernel_sizes", "strides", "paddings"],
            [num_cells, kernel_sizes, strides, paddings],
        ):
            _depth = depth
            setattr(
                self,
                _field,
                (_value if isinstance(_value, Sequence) else [_value] * _depth),
            )
            if not (isinstance(_value, Sequence) or _depth is not None):
                raise RuntimeError(
                    f"If {_field} is provided as an integer, "
                    "depth must be provided too."
                )
            if not (len(getattr(self, _field)) == _depth or _depth is None):
                raise RuntimeError(
                    f"depth={depth} and {_field}={len(getattr(self, _field))} length conflict, "
                    + f"consider matching or specifying a constant {_field} argument together with a a desired depth"
                )

        self.out_features = self.num_cells[-1]

        self.depth = len(self.kernel_sizes)
        layers = self._make_net(device)
        super().__init__(*layers)

    def _make_net(self, device: Optional[DEVICE_TYPING]) -> nn.Module:
        layers = []
        in_features = [self.in_features] + self.num_cells[: self.depth]
        out_features = self.num_cells + [self.out_features]
        kernel_sizes = self.kernel_sizes
        strides = self.strides
        paddings = self.paddings
        for i, (_in, _out, _kernel, _stride, _padding) in enumerate(
            zip(in_features, out_features, kernel_sizes, strides, paddings)
        ):
            _bias = (i < len(in_features) - 1) or self.bias_last_layer
            if _in is not None:
                layers.append(
                    nn.Conv2d(
                        _in,
                        _out,
                        kernel_size=_kernel,
                        stride=_stride,
                        bias=_bias,
                        padding=_padding,
                        device=device,
                    )
                )
            else:
                layers.append(
                    nn.LazyConv2d(
                        _out,
                        kernel_size=_kernel,
                        stride=_stride,
                        bias=_bias,
                        padding=_padding,
                        device=device,
                    )
                )

            layers.append(
                create_on_device(
                    self.activation_class, device, **self.activation_kwargs
                )
            )
            if self.norm_class is not None:
                layers.append(
                    create_on_device(self.norm_class, device, **self.norm_kwargs)
                )

        if self.aggregator_class is not None:
            layers.append(
                create_on_device(
                    self.aggregator_class, device, **self.aggregator_kwargs
                )
            )

        if self.squeeze_output:
            layers.append(Squeeze2dLayer())
        return layers

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        *batch, C, L, W = inputs.shape
        if len(batch) > 1:
            inputs = inputs.flatten(0, len(batch) - 1)
        out = super(ConvNet, self).forward(inputs)
        if len(batch) > 1:
            out = out.unflatten(0, batch)
        return out


Conv2dNet = ConvNet


class Conv3dNet(nn.Sequential):
    """A 3D-convolutional neural network.

    Args:
        in_features (int, optional): number of input features. A lazy implementation that automatically retrieves
            the input size will be used if none is provided.
        depth (int, optional): depth of the network. A depth of 1 will produce a single linear layer network with the
            desired input size, and with an output size equal to the last element of the num_cells argument.
            If no depth is indicated, the depth information should be contained in the num_cells argument (see below).
            If num_cells is an iterable and depth is indicated, both should match: len(num_cells) must be equal to
            the depth.
        num_cells (int or Sequence[int], optional): number of cells of every layer in between the input and output. If
            an integer is provided, every layer will have the same number of cells. If an iterable is provided,
            the linear layers out_features will match the content of num_cells.
            default: ``[32, 32, 32]`` or ``[32] * depth` is depth is not ``None``.
        kernel_sizes (int, Sequence[Union[int, Sequence[int]]]): Kernel size(s) of the conv network. If iterable, the length must match the
            depth, defined by the num_cells or depth arguments.
        strides (int or Sequence[int]): Stride(s) of the conv network. If iterable, the length must match the
            depth, defined by the num_cells or depth arguments.
        activation_class (Type[nn.Module]): activation class to be used.
            default: nn.Tanh
        activation_kwargs (dict, optional): kwargs to be used with the activation class;
        norm_class (Type, optional): normalization class, if any;
        norm_kwargs (dict, optional): kwargs to be used with the normalization layers;
        bias_last_layer (bool): if ``True``, the last Linear layer will have a bias parameter.
            default: True;
        aggregator_class (Type[nn.Module]): aggregator to use at the end of the chain.
            default:  SquashDims;
        aggregator_kwargs (dict, optional): kwargs for the aggregator_class;
        squeeze_output (bool): whether the output should be squeezed of its singleton dimensions.
            default: False.
        device (Optional[DEVICE_TYPING]): device to create the module on.

    Examples:
        >>> # All of the following examples provide valid, working MLPs
        >>> cnet = Conv3dNet(in_features=3, depth=1, num_cells=[32,])
        >>> print(cnet)
        Conv3dNet(
            (0): Conv3d(3, 32, kernel_size=(3, 3, 3), stride=(1, 1, 1))
            (1): ELU(alpha=1.0)
            (2): SquashDims()
        )
        >>> cnet = Conv3dNet(in_features=3, depth=4, num_cells=32)
        >>> print(cnet)
        Conv3dNet(
            (0): Conv3d(3, 32, kernel_size=(3, 3, 3), stride=(1, 1, 1))
            (1): ELU(alpha=1.0)
            (2): Conv3d(32, 32, kernel_size=(3, 3, 3), stride=(1, 1, 1))
            (3): ELU(alpha=1.0)
            (4): Conv3d(32, 32, kernel_size=(3, 3, 3), stride=(1, 1, 1))
            (5): ELU(alpha=1.0)
            (6): Conv3d(32, 32, kernel_size=(3, 3, 3), stride=(1, 1, 1))
            (7): ELU(alpha=1.0)
            (8): SquashDims()
        )
        >>> cnet = Conv3dNet(in_features=3, num_cells=[32, 33, 34, 35])  # defines the depth by the num_cells arg
        >>> print(cnet)
        Conv3dNet(
            (0): Conv3d(3, 32, kernel_size=(3, 3, 3), stride=(1, 1, 1))
            (1): ELU(alpha=1.0)
            (2): Conv3d(32, 33, kernel_size=(3, 3, 3), stride=(1, 1, 1))
            (3): ELU(alpha=1.0)
            (4): Conv3d(33, 34, kernel_size=(3, 3, 3), stride=(1, 1, 1))
            (5): ELU(alpha=1.0)
            (6): Conv3d(34, 35, kernel_size=(3, 3, 3), stride=(1, 1, 1))
            (7): ELU(alpha=1.0)
            (8): SquashDims()
        )
        >>> cnet = Conv3dNet(in_features=3, num_cells=[32, 33, 34, 35], kernel_sizes=[3, 4, 5, (2, 3, 4)])  # defines kernels, possibly rectangular
        >>> print(cnet)
        Conv3dNet(
            (0): Conv3d(3, 32, kernel_size=(3, 3, 3), stride=(1, 1, 1))
            (1): ELU(alpha=1.0)
            (2): Conv3d(32, 33, kernel_size=(4, 4, 4), stride=(1, 1, 1))
            (3): ELU(alpha=1.0)
            (4): Conv3d(33, 34, kernel_size=(5, 5, 5), stride=(1, 1, 1))
            (5): ELU(alpha=1.0)
            (6): Conv3d(34, 35, kernel_size=(2, 3, 4), stride=(1, 1, 1))
            (7): ELU(alpha=1.0)
            (8): SquashDims()
        )

    """

    def __init__(
        self,
        in_features: Optional[int] = None,
        depth: Optional[int] = None,
        num_cells: Union[Sequence, int] = None,
        kernel_sizes: Union[Sequence[Union[int, Sequence[int]]], int] = 3,
        strides: Union[Sequence, int] = 1,
        paddings: Union[Sequence, int] = 0,
        activation_class: Type[nn.Module] = nn.ELU,
        activation_kwargs: Optional[dict] = None,
        norm_class: Optional[Type[nn.Module]] = None,
        norm_kwargs: Optional[dict] = None,
        bias_last_layer: bool = True,
        aggregator_class: Optional[Type[nn.Module]] = SquashDims,
        aggregator_kwargs: Optional[dict] = None,
        squeeze_output: bool = False,
        device: Optional[DEVICE_TYPING] = None,
    ):
        if num_cells is None:
            if depth is None:
                num_cells = [32, 32, 32]
            else:
                num_cells = [32] * depth

        self.in_features = in_features
        self.activation_class = activation_class
        self.activation_kwargs = (
            activation_kwargs if activation_kwargs is not None else {}
        )
        self.norm_class = norm_class
        self.norm_kwargs = norm_kwargs if norm_kwargs is not None else {}
        self.bias_last_layer = bias_last_layer
        self.aggregator_class = aggregator_class
        self.aggregator_kwargs = (
            aggregator_kwargs if aggregator_kwargs is not None else {"ndims_in": 4}
        )
        self.squeeze_output = squeeze_output
        # self.single_bias_last_layer = single_bias_last_layer

        depth = _find_depth(depth, num_cells, kernel_sizes, strides, paddings)
        self.depth = depth
        if depth == 0:
            raise ValueError("Null depth is not permitted with Conv3dNet.")

        for _field, _value in zip(
            ["num_cells", "kernel_sizes", "strides", "paddings"],
            [num_cells, kernel_sizes, strides, paddings],
        ):
            _depth = depth
            setattr(
                self,
                _field,
                (_value if isinstance(_value, Sequence) else [_value] * _depth),
            )
            if not (len(getattr(self, _field)) == _depth or _depth is None):
                raise ValueError(
                    f"depth={depth} and {_field}={len(getattr(self, _field))} length conflict, "
                    + f"consider matching or specifying a constant {_field} argument together with a a desired depth"
                )

        self.out_features = self.num_cells[-1]

        self.depth = len(self.kernel_sizes)
        layers = self._make_net(device)
        super().__init__(*layers)

    def _make_net(self, device: Optional[DEVICE_TYPING]) -> nn.Module:
        layers = []
        in_features = [self.in_features] + self.num_cells[: self.depth]
        out_features = self.num_cells + [self.out_features]
        kernel_sizes = self.kernel_sizes
        strides = self.strides
        paddings = self.paddings
        for i, (_in, _out, _kernel, _stride, _padding) in enumerate(
            zip(in_features, out_features, kernel_sizes, strides, paddings)
        ):
            _bias = (i < len(in_features) - 1) or self.bias_last_layer
            if _in is not None:
                layers.append(
                    nn.Conv3d(
                        _in,
                        _out,
                        kernel_size=_kernel,
                        stride=_stride,
                        bias=_bias,
                        padding=_padding,
                        device=device,
                    )
                )
            else:
                layers.append(
                    nn.LazyConv3d(
                        _out,
                        kernel_size=_kernel,
                        stride=_stride,
                        bias=_bias,
                        padding=_padding,
                        device=device,
                    )
                )

            layers.append(
                create_on_device(
                    self.activation_class, device, **self.activation_kwargs
                )
            )
            if self.norm_class is not None:
                layers.append(
                    create_on_device(self.norm_class, device, **self.norm_kwargs)
                )

        if self.aggregator_class is not None:
            layers.append(
                create_on_device(
                    self.aggregator_class, device, **self.aggregator_kwargs
                )
            )

        if self.squeeze_output:
            layers.append(SqueezeLayer((-3, -2, -1)))
        return layers

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        try:
            *batch, C, D, L, W = inputs.shape
        except ValueError as err:
            raise ValueError(
                f"The input value of {self.__class__.__name__} must have at least 4 dimensions, got {inputs.ndim} instead."
            ) from err
        if len(batch) > 1:
            inputs = inputs.flatten(0, len(batch) - 1)
        out = super().forward(inputs)
        if len(batch) > 1:
            out = out.unflatten(0, batch)
        return out


class DuelingMlpDQNet(nn.Module):
    """Creates a Dueling MLP Q-network.

    Presented in https://arxiv.org/abs/1511.06581

    Args:
        out_features (int): number of features for the advantage network
        out_features_value (int): number of features for the value network
        mlp_kwargs_feature (dict, optional): kwargs for the feature network.
            Default is

            >>> mlp_kwargs_feature = {
            ...     'num_cells': [256, 256],
            ...     'activation_class': nn.ELU,
            ...     'out_features': 256,
            ...     'activate_last_layer': True,
            ... }

        mlp_kwargs_output (dict, optional): kwargs for the advantage and
            value networks.
            Default is

            >>> mlp_kwargs_output = {
            ...     "depth": 1,
            ...     "activation_class": nn.ELU,
            ...     "num_cells": 512,
            ...     "bias_last_layer": True,
            ... }

        device (Optional[DEVICE_TYPING]): device to create the module on.
    """

    def __init__(
        self,
        out_features: int,
        out_features_value: int = 1,
        mlp_kwargs_feature: Optional[dict] = None,
        mlp_kwargs_output: Optional[dict] = None,
        device: Optional[DEVICE_TYPING] = None,
    ):
        super().__init__()

        mlp_kwargs_feature = (
            mlp_kwargs_feature if mlp_kwargs_feature is not None else {}
        )
        _mlp_kwargs_feature = {
            "num_cells": [256, 256],
            "out_features": 256,
            "activation_class": nn.ELU,
            "activate_last_layer": True,
        }
        _mlp_kwargs_feature.update(mlp_kwargs_feature)
        self.features = MLP(device=device, **_mlp_kwargs_feature)

        _mlp_kwargs_output = {
            "depth": 1,
            "activation_class": nn.ELU,
            "num_cells": 512,
            "bias_last_layer": True,
        }
        mlp_kwargs_output = mlp_kwargs_output if mlp_kwargs_output is not None else {}
        _mlp_kwargs_output.update(mlp_kwargs_output)
        self.out_features = out_features
        self.out_features_value = out_features_value
        self.advantage = MLP(
            out_features=out_features, device=device, **_mlp_kwargs_output
        )
        self.value = MLP(
            out_features=out_features_value, device=device, **_mlp_kwargs_output
        )
        for layer in self.modules():
            if isinstance(layer, (nn.Conv2d, nn.Linear)) and isinstance(
                layer.bias, torch.Tensor
            ):
                layer.bias.data.zero_()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.features(x)
        advantage = self.advantage(x)
        value = self.value(x)
        return value + advantage - advantage.mean(dim=-1, keepdim=True)


class DuelingCnnDQNet(nn.Module):
    """Dueling CNN Q-network.

    Presented in https://arxiv.org/abs/1511.06581

    Args:
        out_features (int): number of features for the advantage network
        out_features_value (int): number of features for the value network
        cnn_kwargs (dict, optional): kwargs for the feature network.
            Default is

            >>> cnn_kwargs = {
            ...     'num_cells': [32, 64, 64],
            ...     'strides': [4, 2, 1],
            ...     'kernels': [8, 4, 3],
            ... }

        mlp_kwargs (dict, optional): kwargs for the advantage and value network.
            Default is

            >>> mlp_kwargs = {
            ...     "depth": 1,
            ...     "activation_class": nn.ELU,
            ...     "num_cells": 512,
            ...     "bias_last_layer": True,
            ... }

        device (Optional[DEVICE_TYPING]): device to create the module on.
    """

    def __init__(
        self,
        out_features: int,
        out_features_value: int = 1,
        cnn_kwargs: Optional[dict] = None,
        mlp_kwargs: Optional[dict] = None,
        device: Optional[DEVICE_TYPING] = None,
    ):
        super().__init__()

        cnn_kwargs = cnn_kwargs if cnn_kwargs is not None else {}
        _cnn_kwargs = {
            "num_cells": [32, 64, 64],
            "strides": [4, 2, 1],
            "kernel_sizes": [8, 4, 3],
        }
        _cnn_kwargs.update(cnn_kwargs)
        self.features = ConvNet(device=device, **_cnn_kwargs)

        _mlp_kwargs = {
            "depth": 1,
            "activation_class": nn.ELU,
            "num_cells": 512,
            "bias_last_layer": True,
        }
        mlp_kwargs = mlp_kwargs if mlp_kwargs is not None else {}
        _mlp_kwargs.update(mlp_kwargs)
        self.out_features = out_features
        self.out_features_value = out_features_value
        self.advantage = MLP(out_features=out_features, device=device, **_mlp_kwargs)
        self.value = MLP(out_features=out_features_value, device=device, **_mlp_kwargs)
        for layer in self.modules():
            if isinstance(layer, (nn.Conv2d, nn.Linear)) and isinstance(
                layer.bias, torch.Tensor
            ):
                layer.bias.data.zero_()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.features(x)
        advantage = self.advantage(x)
        value = self.value(x)
        return value + advantage - advantage.mean(dim=-1, keepdim=True)


class DistributionalDQNnet(TensorDictModuleBase):
    """Distributional Deep Q-Network.

    Args:
        DQNet (nn.Module): (deprecated) Q-Network with output length equal
            to the number of atoms:
            output.shape = [*batch, atoms, actions].
        in_keys (list of str or tuples of str): input keys to the log-softmax
            operation. Defaults to ``["action_value"]``.
        out_keys (list of str or tuples of str): output keys to the log-softmax
            operation. Defaults to ``["action_value"]``.

    """

    _wrong_out_feature_dims_error = (
        "DistributionalDQNnet requires dqn output to be at least "
        "2-dimensional, with dimensions *Batch x #Atoms x #Actions. Got {0} "
        "instead."
    )

    def __init__(self, DQNet: nn.Module = None, in_keys=None, out_keys=None):
        super().__init__()
        if DQNet is not None:
            warnings.warn(
                f"Passing a network to {type(self)} is going to be deprecated.",
                category=DeprecationWarning,
            )
            if not (
                not isinstance(DQNet.out_features, Number)
                and len(DQNet.out_features) > 1
            ):
                raise RuntimeError(self._wrong_out_feature_dims_error)
        self.dqn = DQNet
        if in_keys is None:
            in_keys = ["action_value"]
        if out_keys is None:
            out_keys = ["action_value"]
        self.in_keys = in_keys
        self.out_keys = out_keys

    @dispatch(auto_batch_size=False)
    def forward(self, tensordict):
        for in_key, out_key in zip(self.in_keys, self.out_keys):
            q_values = tensordict.get(in_key)
            if self.dqn is not None:
                q_values = self.dqn(q_values)
            if q_values.ndimension() < 2:
                raise RuntimeError(
                    self._wrong_out_feature_dims_error.format(q_values.shape)
                )
            tensordict.set(out_key, F.log_softmax(q_values, dim=-2))
        return tensordict


def ddpg_init_last_layer(
    module: nn.Sequential,
    scale: float = 6e-4,
    device: Optional[DEVICE_TYPING] = None,
) -> None:
    """Initializer for the last layer of DDPG.

    Presented in "CONTINUOUS CONTROL WITH DEEP REINFORCEMENT LEARNING",
    https://arxiv.org/pdf/1509.02971.pdf

    """
    for last_layer in reversed(module):
        if isinstance(last_layer, (nn.Linear, nn.Conv2d)):
            break
    else:
        raise RuntimeError("Could not find a nn.Linear / nn.Conv2d to initialize.")

    last_layer.weight.data.copy_(
        torch.rand_like(last_layer.weight.data, device=device) * scale - scale / 2
    )
    if last_layer.bias is not None:
        last_layer.bias.data.copy_(
            torch.rand_like(last_layer.bias.data, device=device) * scale - scale / 2
        )


class DdpgCnnActor(nn.Module):
    """DDPG Convolutional Actor class.

    Presented in "CONTINUOUS CONTROL WITH DEEP REINFORCEMENT LEARNING",
    https://arxiv.org/pdf/1509.02971.pdf

    The DDPG Convolutional Actor takes as input an observation (some simple transformation of the observed pixels) and
    returns an action vector from it.
    It is trained to maximise the value returned by the DDPG Q Value network.

    Args:
        action_dim (int): length of the action vector.
        conv_net_kwargs (dict, optional): kwargs for the ConvNet.
            default: {
            'in_features': None,
            "num_cells": [32, 64, 64],
            "kernel_sizes": [8, 4, 3],
            "strides": [4, 2, 1],
            "paddings": [0, 0, 1],
            'activation_class': nn.ELU,
            'norm_class': None,
            'aggregator_class': SquashDims,
            'aggregator_kwargs': {"ndims_in": 3},
            'squeeze_output': True,
        }
        mlp_net_kwargs: kwargs for MLP.
            Default: {
            'in_features': None,
            'out_features': action_dim,
            'depth': 2,
            'num_cells': 200,
            'activation_class': nn.ELU,
            'bias_last_layer': True,
        }
        use_avg_pooling (bool, optional): if ``True``, a nn.AvgPooling layer is
            used to aggregate the output. Default is ``False``.
        device (Optional[DEVICE_TYPING]): device to create the module on.
    """

    def __init__(
        self,
        action_dim: int,
        conv_net_kwargs: Optional[dict] = None,
        mlp_net_kwargs: Optional[dict] = None,
        use_avg_pooling: bool = False,
        device: Optional[DEVICE_TYPING] = None,
    ):
        super().__init__()
        conv_net_default_kwargs = {
            "in_features": None,
            "num_cells": [32, 64, 64],
            "kernel_sizes": [8, 4, 3],
            "strides": [4, 2, 1],
            "paddings": [0, 0, 1],
            "activation_class": nn.ELU,
            "norm_class": None,
            "aggregator_class": SquashDims
            if not use_avg_pooling
            else nn.AdaptiveAvgPool2d,
            "aggregator_kwargs": {"ndims_in": 3}
            if not use_avg_pooling
            else {"output_size": (1, 1)},
            "squeeze_output": use_avg_pooling,
        }
        conv_net_kwargs = conv_net_kwargs if conv_net_kwargs is not None else {}
        conv_net_default_kwargs.update(conv_net_kwargs)
        mlp_net_default_kwargs = {
            "in_features": None,
            "out_features": action_dim,
            "depth": 2,
            "num_cells": 200,
            "activation_class": nn.ELU,
            "bias_last_layer": True,
        }
        mlp_net_kwargs = mlp_net_kwargs if mlp_net_kwargs is not None else {}
        mlp_net_default_kwargs.update(mlp_net_kwargs)
        self.convnet = ConvNet(device=device, **conv_net_default_kwargs)
        self.mlp = MLP(device=device, **mlp_net_default_kwargs)
        ddpg_init_last_layer(self.mlp, 6e-4, device=device)

    def forward(self, observation: torch.Tensor) -> Tuple[torch.Tensor, torch.Tensor]:
        hidden = self.convnet(observation)
        action = self.mlp(hidden)
        return action, hidden


class DdpgMlpActor(nn.Module):
    """DDPG Actor class.

    Presented in "CONTINUOUS CONTROL WITH DEEP REINFORCEMENT LEARNING",
    https://arxiv.org/pdf/1509.02971.pdf

    The DDPG Actor takes as input an observation vector and returns an action from it.
    It is trained to maximise the value returned by the DDPG Q Value network.

    Args:
        action_dim (int): length of the action vector
        mlp_net_kwargs (dict, optional): kwargs for MLP.
            Default: {
            'in_features': None,
            'out_features': action_dim,
            'depth': 2,
            'num_cells': [400, 300],
            'activation_class': nn.ELU,
            'bias_last_layer': True,
        }
        device (Optional[DEVICE_TYPING]): device to create the module on.
    """

    def __init__(
        self,
        action_dim: int,
        mlp_net_kwargs: Optional[dict] = None,
        device: Optional[DEVICE_TYPING] = None,
    ):
        super().__init__()
        mlp_net_default_kwargs = {
            "in_features": None,
            "out_features": action_dim,
            "depth": 2,
            "num_cells": [400, 300],
            "activation_class": nn.ELU,
            "bias_last_layer": True,
        }
        mlp_net_kwargs = mlp_net_kwargs if mlp_net_kwargs is not None else {}
        mlp_net_default_kwargs.update(mlp_net_kwargs)
        self.mlp = MLP(device=device, **mlp_net_default_kwargs)
        ddpg_init_last_layer(self.mlp, 6e-3, device=device)

    def forward(self, observation: torch.Tensor) -> torch.Tensor:
        action = self.mlp(observation)
        return action


class DdpgCnnQNet(nn.Module):
    """DDPG Convolutional Q-value class.

    Presented in "CONTINUOUS CONTROL WITH DEEP REINFORCEMENT LEARNING",
    https://arxiv.org/pdf/1509.02971.pdf

    The DDPG Q-value network takes as input an observation and an action, and returns a scalar from it.

    Args:
        conv_net_kwargs (dict, optional): kwargs for the convolutional network.
            default: {
            'in_features': None,
            "num_cells": [32, 64, 128],
            "kernel_sizes": [8, 4, 3],
            "strides": [4, 2, 1],
            "paddings": [0, 0, 1],
            'activation_class': nn.ELU,
            'norm_class': None,
            'aggregator_class': nn.AdaptiveAvgPool2d,
            'aggregator_kwargs': {},
            'squeeze_output': True,
        }
        mlp_net_kwargs (dict, optional): kwargs for MLP.
            Default: {
            'in_features': None,
            'out_features': 1,
            'depth': 2,
            'num_cells': 200,
            'activation_class': nn.ELU,
            'bias_last_layer': True,
        }
        use_avg_pooling (bool, optional): if ``True``, a nn.AvgPooling layer is
            used to aggregate the output. Default is ``True``.
        device (Optional[DEVICE_TYPING]): device to create the module on.
    """

    def __init__(
        self,
        conv_net_kwargs: Optional[dict] = None,
        mlp_net_kwargs: Optional[dict] = None,
        use_avg_pooling: bool = True,
        device: Optional[DEVICE_TYPING] = None,
    ):
        super().__init__()
        conv_net_default_kwargs = {
            "in_features": None,
            "num_cells": [32, 64, 128],
            "kernel_sizes": [8, 4, 3],
            "strides": [4, 2, 1],
            "paddings": [0, 0, 1],
            "activation_class": nn.ELU,
            "norm_class": None,
            "aggregator_class": SquashDims
            if not use_avg_pooling
            else nn.AdaptiveAvgPool2d,
            "aggregator_kwargs": {"ndims_in": 3}
            if not use_avg_pooling
            else {"output_size": (1, 1)},
            "squeeze_output": use_avg_pooling,
        }
        conv_net_kwargs = conv_net_kwargs if conv_net_kwargs is not None else {}
        conv_net_default_kwargs.update(conv_net_kwargs)
        mlp_net_default_kwargs = {
            "in_features": None,
            "out_features": 1,
            "depth": 2,
            "num_cells": 200,
            "activation_class": nn.ELU,
            "bias_last_layer": True,
        }
        mlp_net_kwargs = mlp_net_kwargs if mlp_net_kwargs is not None else {}
        mlp_net_default_kwargs.update(mlp_net_kwargs)
        self.convnet = ConvNet(device=device, **conv_net_default_kwargs)
        self.mlp = MLP(device=device, **mlp_net_default_kwargs)
        ddpg_init_last_layer(self.mlp, 6e-4, device=device)

    def forward(self, observation: torch.Tensor, action: torch.Tensor) -> torch.Tensor:
        hidden = torch.cat([self.convnet(observation), action], -1)
        value = self.mlp(hidden)
        return value


class DdpgMlpQNet(nn.Module):
    """DDPG Q-value MLP class.

    Presented in "CONTINUOUS CONTROL WITH DEEP REINFORCEMENT LEARNING",
    https://arxiv.org/pdf/1509.02971.pdf

    The DDPG Q-value network takes as input an observation and an action, and returns a scalar from it.
    Because actions are integrated later than observations, two networks are created.

    Args:
        mlp_net_kwargs_net1 (dict, optional): kwargs for MLP.
            Default: {
                'in_features': None,
                'out_features': 400,
                'depth': 0,
                'num_cells': [],
                'activation_class': nn.ELU,
                'bias_last_layer': True,
                'activate_last_layer': True,
            }
        mlp_net_kwargs_net2
            Default: {
                'in_features': None,
                'out_features': 1,
                'depth': 1,
                'num_cells': [300, ],
                'activation_class': nn.ELU,
                'bias_last_layer': True,
            }
        device (Optional[DEVICE_TYPING]): device to create the module on.
    """

    def __init__(
        self,
        mlp_net_kwargs_net1: Optional[dict] = None,
        mlp_net_kwargs_net2: Optional[dict] = None,
        device: Optional[DEVICE_TYPING] = None,
    ):
        super().__init__()
        mlp1_net_default_kwargs = {
            "in_features": None,
            "out_features": 400,
            "depth": 0,
            "num_cells": [],
            "activation_class": nn.ELU,
            "bias_last_layer": True,
            "activate_last_layer": True,
        }
        mlp_net_kwargs_net1: Dict = (
            mlp_net_kwargs_net1 if mlp_net_kwargs_net1 is not None else {}
        )
        mlp1_net_default_kwargs.update(mlp_net_kwargs_net1)
        self.mlp1 = MLP(device=device, **mlp1_net_default_kwargs)

        mlp2_net_default_kwargs = {
            "in_features": None,
            "out_features": 1,
            "num_cells": [
                300,
            ],
            "activation_class": nn.ELU,
            "bias_last_layer": True,
        }
        mlp_net_kwargs_net2 = (
            mlp_net_kwargs_net2 if mlp_net_kwargs_net2 is not None else {}
        )
        mlp2_net_default_kwargs.update(mlp_net_kwargs_net2)
        self.mlp2 = MLP(device=device, **mlp2_net_default_kwargs)
        ddpg_init_last_layer(self.mlp2, 6e-3, device=device)

    def forward(self, observation: torch.Tensor, action: torch.Tensor) -> torch.Tensor:
        value = self.mlp2(torch.cat([self.mlp1(observation), action], -1))
        return value


class LSTMNet(nn.Module):
    """An embedder for an LSTM preceded by an MLP.

    The forward method returns the hidden states of the current state (input hidden states) and the output, as
    the environment returns the 'observation' and 'next_observation'.

    Because the LSTM kernel only returns the last hidden state, hidden states
    are padded with zeros such that they have the right size to be stored in a
    TensorDict of size [batch x time_steps].

    If a 2D tensor is provided as input, it is assumed that it is a batch of data
    with only one time step. This means that we explicitely assume that users will
    unsqueeze inputs of a single batch with multiple time steps.

    Examples:
        >>> batch = 7
        >>> time_steps = 6
        >>> in_features = 4
        >>> out_features = 10
        >>> hidden_size = 5
        >>> net = LSTMNet(
        ...     out_features,
        ...     {"input_size": hidden_size, "hidden_size": hidden_size},
        ...     {"out_features": hidden_size},
        ... )
        >>> # test single step vs multi-step
        >>> x = torch.randn(batch, time_steps, in_features)  # >3 dims = multi-step
        >>> y, hidden0_in, hidden1_in, hidden0_out, hidden1_out = net(x)
        >>> x = torch.randn(batch, in_features)  # 2 dims = single step
        >>> y, hidden0_in, hidden1_in, hidden0_out, hidden1_out = net(x)

    """

    def __init__(
        self,
        out_features: int,
        lstm_kwargs: Dict,
        mlp_kwargs: Dict,
        device: Optional[DEVICE_TYPING] = None,
    ) -> None:
        warnings.warn(
            "LSTMNet is being deprecated in favour of torchrl.modules.LSTMModule, and will be removed soon.",
            category=DeprecationWarning,
        )
        super().__init__()
        lstm_kwargs.update({"batch_first": True})
        self.mlp = MLP(device=device, **mlp_kwargs)
        self.lstm = nn.LSTM(device=device, **lstm_kwargs)
        self.linear = nn.LazyLinear(out_features, device=device)

    def _lstm(
        self,
        input: torch.Tensor,
        hidden0_in: Optional[torch.Tensor] = None,
        hidden1_in: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        squeeze0 = False
        squeeze1 = False
        if input.ndimension() == 1:
            squeeze0 = True
            input = input.unsqueeze(0).contiguous()

        if input.ndimension() == 2:
            squeeze1 = True
            input = input.unsqueeze(1).contiguous()
        batch, steps = input.shape[:2]

        if hidden1_in is None and hidden0_in is None:
            shape = (batch, steps) if not squeeze1 else (batch,)
            hidden0_in, hidden1_in = [
                torch.zeros(
                    *shape,
                    self.lstm.num_layers,
                    self.lstm.hidden_size,
                    device=input.device,
                    dtype=input.dtype,
                )
                for _ in range(2)
            ]
        elif hidden1_in is None or hidden0_in is None:
            raise RuntimeError(
                f"got type(hidden0)={type(hidden0_in)} and type(hidden1)={type(hidden1_in)}"
            )
        elif squeeze0:
            hidden0_in = hidden0_in.unsqueeze(0)
            hidden1_in = hidden1_in.unsqueeze(0)

        # we only need the first hidden state
        if not squeeze1:
            _hidden0_in = hidden0_in[:, 0]
            _hidden1_in = hidden1_in[:, 0]
        else:
            _hidden0_in = hidden0_in
            _hidden1_in = hidden1_in
        hidden = (
            _hidden0_in.transpose(-3, -2).contiguous(),
            _hidden1_in.transpose(-3, -2).contiguous(),
        )

        y0, hidden = self.lstm(input, hidden)
        # dim 0 in hidden is num_layers, but that will conflict with tensordict
        hidden = tuple(_h.transpose(0, 1) for _h in hidden)
        y = self.linear(y0)

        out = [y, hidden0_in, hidden1_in, *hidden]
        if squeeze1:
            # squeezes time
            out[0] = out[0].squeeze(1)
        if not squeeze1:
            # we pad the hidden states with zero to make tensordict happy
            for i in range(3, 5):
                out[i] = torch.stack(
                    [torch.zeros_like(out[i]) for _ in range(input.shape[1] - 1)]
                    + [out[i]],
                    1,
                )
        if squeeze0:
            out = [_out.squeeze(0) for _out in out]
        return tuple(out)

    def forward(
        self,
        input: torch.Tensor,
        hidden0_in: Optional[torch.Tensor] = None,
        hidden1_in: Optional[torch.Tensor] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        input = self.mlp(input)
        return self._lstm(input, hidden0_in, hidden1_in)


class OnlineDTActor(nn.Module):
    """Online Decision Transformer Actor class.

    Actor class for the Online Decision Transformer to sample actions from gaussian distribution as presented inresented in `"Online Decision Transformer" <https://arxiv.org/abs/2202.05607.pdf>`.
    Returns mu and sigma for the gaussian distribution to sample actions from.

    Args:
        state_dim (int): state dimension.
        action_dim (int): action dimension.
        transformer_config (Dict or :class:`DecisionTransformer.DTConfig`):
            config for the GPT2 transformer.
            Defaults to :meth:`~.default_config`.
        device (Optional[DEVICE_TYPING], optional): device to use. Defaults to None.

    Examples:
        >>> model = OnlineDTActor(state_dim=4, action_dim=2,
        ...     transformer_config=OnlineDTActor.default_config())
        >>> observation = torch.randn(32, 10, 4)
        >>> action = torch.randn(32, 10, 2)
        >>> return_to_go = torch.randn(32, 10, 1)
        >>> mu, std = model(observation, action, return_to_go)
        >>> mu.shape
        torch.Size([32, 10, 2])
        >>> std.shape
        torch.Size([32, 10, 2])
    """

    def __init__(
        self,
        state_dim: int,
        action_dim: int,
        transformer_config: Dict | DecisionTransformer.DTConfig = None,
        device: Optional[DEVICE_TYPING] = None,
    ):
        super().__init__()
        if transformer_config is None:
            transformer_config = self.default_config()
        if isinstance(transformer_config, DecisionTransformer.DTConfig):
            transformer_config = dataclasses.asdict(transformer_config)
        self.transformer = DecisionTransformer(
            state_dim=state_dim,
            action_dim=action_dim,
            config=transformer_config,
        )
        self.action_layer_mean = nn.Linear(
            transformer_config["n_embd"], action_dim, device=device
        )
        self.action_layer_logstd = nn.Linear(
            transformer_config["n_embd"], action_dim, device=device
        )

        self.log_std_min, self.log_std_max = -5.0, 2.0

        def weight_init(m):
            """Custom weight init for Conv2D and Linear layers."""
            if isinstance(m, torch.nn.Linear):
                nn.init.orthogonal_(m.weight.data)
                if hasattr(m.bias, "data"):
                    m.bias.data.fill_(0.0)

        self.action_layer_mean.apply(weight_init)
        self.action_layer_logstd.apply(weight_init)

    def forward(
        self,
        observation: torch.Tensor,
        action: torch.Tensor,
        return_to_go: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        hidden_state = self.transformer(observation, action, return_to_go)
        mu = self.action_layer_mean(hidden_state)
        log_std = self.action_layer_logstd(hidden_state)

        log_std = torch.tanh(log_std)
        # log_std is the output of tanh so it will be between [-1, 1]
        # map it to be between [log_std_min, log_std_max]
        log_std = self.log_std_min + 0.5 * (self.log_std_max - self.log_std_min) * (
            log_std + 1.0
        )
        std = log_std.exp()

        return mu, std

    @classmethod
    def default_config(cls):
        """Default configuration for :class:`~.OnlineDTActor`."""
        return DecisionTransformer.DTConfig(
            n_embd=512,
            n_layer=4,
            n_head=4,
            n_inner=2048,
            activation="relu",
            n_positions=1024,
            resid_pdrop=0.1,
            attn_pdrop=0.1,
        )


class DTActor(nn.Module):
    """Decision Transformer Actor class.

    Actor class for the Decision Transformer to output deterministic action as presented in `"Decision Transformer" <https://arxiv.org/abs/2202.05607.pdf>`.
    Returns the deterministic actions.

    Args:
        state_dim (int): state dimension.
        action_dim (int): action dimension.
        transformer_config (Dict or :class:`DecisionTransformer.DTConfig`, optional):
            config for the GPT2 transformer.
            Defaults to :meth:`~.default_config`.
        device (Optional[DEVICE_TYPING], optional): device to use. Defaults to None.

    Examples:
        >>> model = DTActor(state_dim=4, action_dim=2,
        ...     transformer_config=DTActor.default_config())
        >>> observation = torch.randn(32, 10, 4)
        >>> action = torch.randn(32, 10, 2)
        >>> return_to_go = torch.randn(32, 10, 1)
        >>> output = model(observation, action, return_to_go)
        >>> output.shape
        torch.Size([32, 10, 2])

    """

    def __init__(
        self,
        state_dim: int,
        action_dim: int,
        transformer_config: Dict | DecisionTransformer.DTConfig = None,
        device: Optional[DEVICE_TYPING] = None,
    ):
        super().__init__()
        if transformer_config is None:
            transformer_config = self.default_config()
        if isinstance(transformer_config, DecisionTransformer.DTConfig):
            transformer_config = dataclasses.asdict(transformer_config)
        self.transformer = DecisionTransformer(
            state_dim=state_dim,
            action_dim=action_dim,
            config=transformer_config,
        )
        self.action_layer = nn.Linear(
            transformer_config["n_embd"], action_dim, device=device
        )

        def weight_init(m):
            """Custom weight init for Conv2D and Linear layers."""
            if isinstance(m, torch.nn.Linear):
                nn.init.orthogonal_(m.weight.data)
                if hasattr(m.bias, "data"):
                    m.bias.data.fill_(0.0)

        self.action_layer.apply(weight_init)

    def forward(
        self,
        observation: torch.Tensor,
        action: torch.Tensor,
        return_to_go: torch.Tensor,
    ) -> torch.Tensor:
        hidden_state = self.transformer(observation, action, return_to_go)
        out = self.action_layer(hidden_state)
        return out

    @classmethod
    def default_config(cls):
        """Default configuration for :class:`~.DTActor`."""
        return DecisionTransformer.DTConfig(
            n_embd=512,
            n_layer=4,
            n_head=4,
            n_inner=2048,
            activation="relu",
            n_positions=1024,
            resid_pdrop=0.1,
            attn_pdrop=0.1,
        )
