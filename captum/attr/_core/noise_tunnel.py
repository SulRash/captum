#!/usr/bin/env python3

# pyre-strict
from enum import Enum
from typing import Any, Callable, cast, List, Optional, Tuple, Union

import torch
from captum._utils.common import (
    _expand_and_update_additional_forward_args,
    _expand_and_update_baselines,
    _expand_and_update_feature_mask,
    _expand_and_update_target,
    _format_output,
    _format_tensor_into_tuples,
    _is_tuple,
)
from captum._utils.typing import TensorOrTupleOfTensorsGeneric
from captum.attr._utils.attribution import Attribution, GradientAttribution
from captum.attr._utils.common import _validate_noise_tunnel_type
from captum.log import log_usage
from torch import Tensor


class NoiseTunnelType(Enum):
    smoothgrad = 1
    smoothgrad_sq = 2
    vargrad = 3


# pyre-fixme[5]: Global expression must be annotated.
SUPPORTED_NOISE_TUNNEL_TYPES = list(NoiseTunnelType.__members__.keys())


class NoiseTunnel(Attribution):
    r"""
    Adds gaussian noise to each input in the batch `nt_samples` times
    and applies the given attribution algorithm to each of the samples.
    The attributions of the samples are combined based on the given noise
    tunnel type (nt_type):
    If nt_type is `smoothgrad`, the mean of the sampled attributions is
    returned. This approximates smoothing the given attribution method
    with a Gaussian Kernel.
    If nt_type is `smoothgrad_sq`, the mean of the squared sample attributions
    is returned.
    If nt_type is `vargrad`, the variance of the sample attributions is
    returned.

    More details about adding noise can be found in the following papers:

        * https://arxiv.org/abs/1810.03292
        * https://arxiv.org/abs/1810.03307
        * https://arxiv.org/abs/1706.03825
        * https://arxiv.org/abs/1806.10758

    This method currently also supports batches of multiple examples input,
    however it can be computationally expensive depending on the model,
    the dimensionality of the data and execution environment.
    It is assumed that the batch size is the first dimension of input tensors.
    """

    def __init__(self, attribution_method: Attribution) -> None:
        r"""
        Args:
            attribution_method (Attribution): An instance of any attribution algorithm
                        of type `Attribution`. E.g. Integrated Gradients,
                        Conductance or Saliency.
        """
        self.attribution_method = attribution_method
        # pyre-fixme[4]: Attribute must be annotated.
        self.is_delta_supported = self.attribution_method.has_convergence_delta()
        # pyre-fixme[4]: Attribute must be annotated.
        self._multiply_by_inputs = self.attribution_method.multiplies_by_inputs
        # pyre-fixme[4]: Attribute must be annotated.
        self.is_gradient_method = isinstance(
            self.attribution_method, GradientAttribution
        )
        Attribution.__init__(self, self.attribution_method.forward_func)

    @property
    # pyre-fixme[3]: Return type must be annotated.
    def multiplies_by_inputs(self):
        return self._multiply_by_inputs

    @log_usage()
    def attribute(
        self,
        inputs: Union[Tensor, Tuple[Tensor, ...]],
        nt_type: str = "smoothgrad",
        nt_samples: int = 5,
        nt_samples_batch_size: Optional[int] = None,
        stdevs: Union[float, Tuple[float, ...]] = 1.0,
        draw_baseline_from_distrib: bool = False,
        **kwargs: Any,
    ) -> Union[
        Union[
            Tensor,
            Tuple[Tensor, Tensor],
            Tuple[Tensor, ...],
            Tuple[Tuple[Tensor, ...], Tensor],
        ]
    ]:
        r"""
        Args:

            inputs (Tensor or tuple[Tensor, ...]): Input for which integrated
                        gradients are computed. If forward_func takes a single
                        tensor as input, a single input tensor should be provided.
                        If forward_func takes multiple tensors as input, a tuple
                        of the input tensors should be provided. It is assumed
                        that for all given input tensors, dimension 0 corresponds
                        to the number of examples, and if multiple input tensors
                        are provided, the examples must be aligned appropriately.
            nt_type (str, optional): Smoothing type of the attributions.
                        `smoothgrad`, `smoothgrad_sq` or `vargrad`
                        Default: `smoothgrad` if `type` is not provided.
            nt_samples (int, optional): The number of randomly generated examples
                        per sample in the input batch. Random examples are
                        generated by adding gaussian random noise to each sample.
                        Default: `5` if `nt_samples` is not provided.
            nt_samples_batch_size (int, optional): The number of the `nt_samples`
                        that will be processed together. With the help
                        of this parameter we can avoid out of memory situation and
                        reduce the number of randomly generated examples per sample
                        in each batch.
                        Default: None if `nt_samples_batch_size` is not provided. In
                        this case all `nt_samples` will be processed together.
            stdevs    (float or tuple of float, optional): The standard deviation
                        of gaussian noise with zero mean that is added to each
                        input in the batch. If `stdevs` is a single float value
                        then that same value is used for all inputs. If it is
                        a tuple, then it must have the same length as the inputs
                        tuple. In this case, each stdev value in the stdevs tuple
                        corresponds to the input with the same index in the inputs
                        tuple.
                        Default: `1.0` if `stdevs` is not provided.
            draw_baseline_from_distrib (bool, optional): Indicates whether to
                        randomly draw baseline samples from the `baselines`
                        distribution provided as an input tensor.
                        Default: False
            **kwargs (Any, optional): Contains a list of arguments that are passed
                        to `attribution_method` attribution algorithm.
                        Any additional arguments that should be used for the
                        chosen attribution method should be included here.
                        For instance, such arguments include
                        `additional_forward_args` and `baselines`.

        Returns:
            **attributions** or 2-element tuple of **attributions**, **delta**:
            - **attributions** (*Tensor* or *tuple[Tensor, ...]*):
                        Attribution with
                        respect to each input feature. attributions will always be
                        the same size as the provided inputs, with each value
                        providing the attribution of the corresponding input index.
                        If a single tensor is provided as inputs, a single tensor is
                        returned. If a tuple is provided for inputs, a tuple of
                        corresponding sized tensors is returned.
            - **delta** (*float*, returned if return_convergence_delta=True):
                        Approximation error computed by the
                        attribution algorithm. Not all attribution algorithms
                        return delta value. It is computed only for some
                        algorithms, e.g. integrated gradients.
                        Delta is computed for each input in the batch
                        and represents the arithmetic mean
                        across all `nt_samples` perturbed tensors for that input.


        Examples::

            >>> # ImageClassifier takes a single input tensor of images Nx3x32x32,
            >>> # and returns an Nx10 tensor of class probabilities.
            >>> net = ImageClassifier()
            >>> ig = IntegratedGradients(net)
            >>> input = torch.randn(2, 3, 32, 32, requires_grad=True)
            >>> # Creates noise tunnel
            >>> nt = NoiseTunnel(ig)
            >>> # Generates 10 perturbed input tensors per image.
            >>> # Computes integrated gradients for class 3 for each generated
            >>> # input and averages attributions across all 10
            >>> # perturbed inputs per image
            >>> attribution = nt.attribute(input, nt_type='smoothgrad',
            >>>                            nt_samples=10, target=3)
        """

        def add_noise_to_inputs(nt_samples_partition: int) -> Tuple[Tensor, ...]:
            if isinstance(stdevs, tuple):
                assert len(stdevs) == len(inputs), (
                    "The number of input tensors "
                    "in {} must be equal to the number of stdevs values {}".format(
                        len(inputs), len(stdevs)
                    )
                )
            else:
                assert isinstance(
                    stdevs, float
                ), "stdevs must be type float. " "Given: {}".format(type(stdevs))
                stdevs_ = (stdevs,) * len(inputs)
            return tuple(
                (
                    add_noise_to_input(
                        input, stdev, nt_samples_partition
                    ).requires_grad_()
                    if self.is_gradient_method
                    else add_noise_to_input(input, stdev, nt_samples_partition)
                )
                # pyre-fixme[61]: `stdevs_` is undefined, or not always defined.
                for (input, stdev) in zip(inputs, stdevs_)
            )

        def add_noise_to_input(
            input: Tensor, stdev: float, nt_samples_partition: int
        ) -> Tensor:
            # batch size
            bsz = input.shape[0]

            # expand input size by the number of drawn samples
            # pyre-fixme[58]: `+` is not supported for operand types `Tuple[int]`
            #  and `Size`.
            input_expanded_size = (bsz * nt_samples_partition,) + input.shape[1:]

            # expand stdev for the shape of the input and number of drawn samples
            stdev_expanded = torch.tensor(stdev, device=input.device).repeat(
                input_expanded_size
            )

            # draws `np.prod(input_expanded_size)` samples from normal distribution
            # with given input parametrization
            # FIXME it look like it is very difficult to make torch.normal
            # deterministic this needs an investigation
            noise = torch.normal(0, stdev_expanded)
            return input.repeat_interleave(nt_samples_partition, dim=0) + noise

        def update_sum_attribution_and_sq(
            sum_attribution: List[Tensor],
            sum_attribution_sq: List[Tensor],
            attribution: Tensor,
            i: int,
            nt_samples_batch_size_inter: int,
        ) -> None:
            bsz = attribution.shape[0] // nt_samples_batch_size_inter
            attribution_shape = cast(
                Tuple[int, ...], (bsz, nt_samples_batch_size_inter)
            )
            if len(attribution.shape) > 1:
                # pyre-fixme[22]: The cast is redundant.
                attribution_shape += cast(Tuple[int, ...], tuple(attribution.shape[1:]))

            attribution = attribution.view(attribution_shape)
            current_attribution_sum = attribution.sum(dim=1, keepdim=False)
            # pyre-fixme[58]: `**` is not supported for operand types `Tensor` and
            #  `int`.
            current_attribution_sq = torch.sum(attribution**2, dim=1, keepdim=False)

            sum_attribution[i] = (
                current_attribution_sum
                if not isinstance(sum_attribution[i], torch.Tensor)
                else sum_attribution[i] + current_attribution_sum
            )
            sum_attribution_sq[i] = (
                current_attribution_sq
                if not isinstance(sum_attribution_sq[i], torch.Tensor)
                else sum_attribution_sq[i] + current_attribution_sq
            )

        def compute_partial_attribution(
            inputs_with_noise_partition: Tuple[Tensor, ...],
            # pyre-fixme[2]: Parameter annotation cannot be `Any`.
            kwargs_partition: Any,
        ) -> Tuple[Tuple[Tensor, ...], bool, Union[None, Tensor]]:
            # smoothgrad_Attr(x) = 1 / n * sum(Attr(x + N(0, sigma^2))
            # NOTE: using __wrapped__ such that it does not log the inner logs

            attributions = attr_func.__wrapped__(  # type: ignore
                self.attribution_method,  # self
                (
                    inputs_with_noise_partition
                    if is_inputs_tuple
                    else inputs_with_noise_partition[0]
                ),
                **kwargs_partition,
            )
            delta = None

            if self.is_delta_supported and return_convergence_delta:
                attributions, delta = attributions

            is_attrib_tuple = _is_tuple(attributions)
            attributions = _format_tensor_into_tuples(attributions)

            return (
                cast(Tuple[Tensor, ...], attributions),
                cast(bool, is_attrib_tuple),
                delta,
            )

        # pyre-fixme[24]: Generic type `dict` expects 2 type parameters, use
        #  `typing.Dict[<key type>, <value type>]` to avoid runtime subscripting
        #  errors.
        def expand_partial(nt_samples_partition: int, kwargs_partial: dict) -> None:
            # if the algorithm supports targets, baselines and/or
            # additional_forward_args they will be expanded based
            # on the nt_samples_partition and corresponding kwargs
            # variables will be updated accordingly
            _expand_and_update_additional_forward_args(
                nt_samples_partition, kwargs_partial
            )
            _expand_and_update_target(nt_samples_partition, kwargs_partial)
            _expand_and_update_baselines(
                cast(Tuple[Tensor, ...], inputs),
                nt_samples_partition,
                kwargs_partial,
                draw_baseline_from_distrib=draw_baseline_from_distrib,
            )
            _expand_and_update_feature_mask(nt_samples_partition, kwargs_partial)

        def compute_smoothing(
            expected_attributions: Tuple[Union[Tensor], ...],
            expected_attributions_sq: Tuple[Union[Tensor], ...],
        ) -> Tuple[Tensor, ...]:
            if NoiseTunnelType[nt_type] == NoiseTunnelType.smoothgrad:
                return expected_attributions

            if NoiseTunnelType[nt_type] == NoiseTunnelType.smoothgrad_sq:
                return expected_attributions_sq

            vargrad = tuple(
                expected_attribution_sq - expected_attribution * expected_attribution
                for expected_attribution, expected_attribution_sq in zip(
                    expected_attributions, expected_attributions_sq
                )
            )

            # pyre-fixme[22]: The cast is redundant.
            return cast(Tuple[Tensor, ...], vargrad)

        def update_partial_attribution_and_delta(
            attributions_partial: Tuple[Tensor, ...],
            delta_partial: Tensor,
            sum_attributions: List[Tensor],
            sum_attributions_sq: List[Tensor],
            delta_partial_list: List[Tensor],
            nt_samples_partial: int,
        ) -> None:
            for i, attribution_partial in enumerate(attributions_partial):
                update_sum_attribution_and_sq(
                    sum_attributions,
                    sum_attributions_sq,
                    attribution_partial,
                    i,
                    nt_samples_partial,
                )
            if self.is_delta_supported and return_convergence_delta:
                delta_partial_list.append(delta_partial)

        return_convergence_delta: bool
        return_convergence_delta = (
            "return_convergence_delta" in kwargs and kwargs["return_convergence_delta"]
        )
        with torch.no_grad():
            nt_samples_batch_size = (
                nt_samples
                if nt_samples_batch_size is None
                else min(nt_samples, nt_samples_batch_size)
            )

            nt_samples_partition = nt_samples // nt_samples_batch_size

            # Keeps track whether original input is a tuple or not before
            # converting it into a tuple.
            is_inputs_tuple = isinstance(inputs, tuple)

            inputs = _format_tensor_into_tuples(inputs)  # type: ignore

            _validate_noise_tunnel_type(nt_type, SUPPORTED_NOISE_TUNNEL_TYPES)

            kwargs_copy = kwargs.copy()
            expand_partial(nt_samples_batch_size, kwargs_copy)

            attr_func = self.attribution_method.attribute

            sum_attributions: List[Union[None, Tensor]] = []
            sum_attributions_sq: List[Union[None, Tensor]] = []
            delta_partial_list: List[Tensor] = []

            for _ in range(nt_samples_partition):
                inputs_with_noise = add_noise_to_inputs(nt_samples_batch_size)
                (
                    attributions_partial,
                    is_attrib_tuple,
                    delta_partial,
                ) = compute_partial_attribution(inputs_with_noise, kwargs_copy)

                if len(sum_attributions) == 0:
                    # pyre-fixme[9]: sum_attributions has type
                    #  `List[Optional[Tensor]]`; used as `List[None]`.
                    sum_attributions = [None] * len(attributions_partial)
                    # pyre-fixme[9]: sum_attributions_sq has type
                    #  `List[Optional[Tensor]]`; used as `List[None]`.
                    sum_attributions_sq = [None] * len(attributions_partial)

                update_partial_attribution_and_delta(
                    # pyre-fixme[22]: The cast is redundant.
                    cast(Tuple[Tensor, ...], attributions_partial),
                    cast(Tensor, delta_partial),
                    cast(List[Tensor], sum_attributions),
                    cast(List[Tensor], sum_attributions_sq),
                    delta_partial_list,
                    nt_samples_batch_size,
                )

            nt_samples_remaining = (
                nt_samples - nt_samples_partition * nt_samples_batch_size
            )
            if nt_samples_remaining > 0:
                inputs_with_noise = add_noise_to_inputs(nt_samples_remaining)
                expand_partial(nt_samples_remaining, kwargs)
                (
                    attributions_partial,
                    is_attrib_tuple,
                    delta_partial,
                ) = compute_partial_attribution(inputs_with_noise, kwargs)

                update_partial_attribution_and_delta(
                    # pyre-fixme[22]: The cast is redundant.
                    cast(Tuple[Tensor, ...], attributions_partial),
                    cast(Tensor, delta_partial),
                    cast(List[Tensor], sum_attributions),
                    cast(List[Tensor], sum_attributions_sq),
                    delta_partial_list,
                    nt_samples_remaining,
                )

            expected_attributions = tuple(
                [
                    cast(Tensor, sum_attribution) * 1 / nt_samples
                    for sum_attribution in sum_attributions
                ]
            )
            expected_attributions_sq = tuple(
                [
                    cast(Tensor, sum_attribution_sq) * 1 / nt_samples
                    for sum_attribution_sq in sum_attributions_sq
                ]
            )
            attributions = compute_smoothing(
                # pyre-fixme[22]: The cast is redundant.
                cast(Tuple[Tensor, ...], expected_attributions),
                # pyre-fixme[22]: The cast is redundant.
                cast(Tuple[Tensor, ...], expected_attributions_sq),
            )

            delta = None
            if self.is_delta_supported and return_convergence_delta:
                delta = torch.cat(delta_partial_list, dim=0)

        return self._apply_checks_and_return_attributions(
            attributions,
            # pyre-fixme[61]: `is_attrib_tuple` is undefined, or not always defined.
            is_attrib_tuple,
            return_convergence_delta,
            delta,
        )

    # pyre-fixme[24] Generic type `Callable` expects 2 type parameters.
    def attribute_future(self) -> Callable:
        r"""
        This method is not implemented for NoiseTunnel.
        """
        raise NotImplementedError("attribute_future is not implemented for NoiseTunnel")

    def _apply_checks_and_return_attributions(
        self,
        attributions: Tuple[Tensor, ...],
        is_attrib_tuple: bool,
        return_convergence_delta: bool,
        delta: Union[None, Tensor],
        # pyre-fixme[34]: `Variable[TensorOrTupleOfTensorsGeneric <:
        #  [torch._tensor.Tensor, typing.Tuple[torch._tensor.Tensor, ...]]]`
        #  isn't present in the function's parameters.
    ) -> Union[
        TensorOrTupleOfTensorsGeneric, Tuple[TensorOrTupleOfTensorsGeneric, Tensor]
    ]:
        # pyre-fixme[9]: Unable to unpack `Union[Tensor, typing.Tuple[Tensor,
        #  ...]]`, expected a tuple.
        attributions = _format_output(is_attrib_tuple, attributions)

        ret = (
            (attributions, cast(Tensor, delta))
            if self.is_delta_supported and return_convergence_delta
            else attributions
        )
        ret = cast(
            # pyre-fixme[34]: `Variable[TensorOrTupleOfTensorsGeneric <:
            #  [torch._tensor.Tensor, typing.Tuple[torch._tensor.Tensor, ...]]]`
            # isn't present in the function's parameters.
            Union[
                TensorOrTupleOfTensorsGeneric,
                Tuple[TensorOrTupleOfTensorsGeneric, Tensor],
            ],
            ret,
        )
        return ret

    def has_convergence_delta(self) -> bool:
        return self.is_delta_supported
