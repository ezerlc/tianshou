import torch
import numpy as np
from typing import Any, Dict, Union, Optional

from tianshou.policy import DQNPolicy
from tianshou.data import Batch, ReplayBuffer, to_numpy


class C51Policy(DQNPolicy):
    """Implementation of Categorical Deep Q-Network. arXiv:1707.06887.

    :param torch.nn.Module model: a model following the rules in
        :class:`~tianshou.policy.BasePolicy`. (s -> logits)
    :param torch.optim.Optimizer optim: a torch.optim for optimizing the model.
    :param float discount_factor: in [0, 1].
    :param int num_atoms: the number of atoms in the support set of the
        value distribution, defaults to 51.
    :param float v_min: the value of the smallest atom in the support set,
        defaults to -10.0.
    :param float v_max: the value of the largest atom in the support set,
        defaults to 10.0.
    :param int estimation_step: greater than 1, the number of steps to look
        ahead.
    :param int target_update_freq: the target network update frequency (0 if
        you do not use the target network).
    :param bool reward_normalization: normalize the reward to Normal(0, 1),
        defaults to False.

    .. seealso::

        Please refer to :class:`~tianshou.policy.DQNPolicy` for more detailed
        explanation.
    """

    def __init__(
        self,
        model: torch.nn.Module,
        optim: torch.optim.Optimizer,
        discount_factor: float = 0.99,
        num_atoms: int = 51,
        v_min: float = -10.0,
        v_max: float = 10.0,
        estimation_step: int = 1,
        target_update_freq: int = 0,
        reward_normalization: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(model, optim, discount_factor, estimation_step,
                         target_update_freq, reward_normalization, **kwargs)
        assert num_atoms > 1, "num_atoms should be greater than 1"
        assert v_min < v_max, "v_max should be larger than v_min"
        self._num_atoms = num_atoms
        self._v_min = v_min
        self._v_max = v_max
        self.support = torch.nn.Parameter(
            torch.linspace(self._v_min, self._v_max, self._num_atoms),
            requires_grad=False,
        )
        self.delta_z = (v_max - v_min) / (num_atoms - 1)

    def _target_q(
        self, buffer: ReplayBuffer, indice: np.ndarray
    ) -> torch.Tensor:
        return self.support.repeat(len(indice), 1)  # shape: [bsz, num_atoms]

    def forward(
        self,
        batch: Batch,
        state: Optional[Union[dict, Batch, np.ndarray]] = None,
        model: str = "model",
        input: str = "obs",
        **kwargs: Any,
    ) -> Batch:
        """Compute action over the given batch data.

        :return: A :class:`~tianshou.data.Batch` which has 2 keys:

            * ``act`` the action.
            * ``state`` the hidden state.

        .. seealso::

            Please refer to :meth:`~tianshou.policy.DQNPolicy.forward` for
            more detailed explanation.
        """
        model = getattr(self, model)
        obs = batch[input]
        obs_ = obs.obs if hasattr(obs, "obs") else obs
        dist, h = model(obs_, state=state, info=batch.info)
        q = (dist * self.support).sum(2)
        act: np.ndarray = to_numpy(q.max(dim=1)[1])
        if hasattr(obs, "mask"):
            # some of actions are masked, they cannot be selected
            q_: np.ndarray = to_numpy(q)
            q_[~obs.mask] = -np.inf
            act = q_.argmax(axis=1)
        # add eps to act in training or testing phase
        if not self.updating and not np.isclose(self.eps, 0.0):
            for i in range(len(q)):
                if np.random.rand() < self.eps:
                    q_ = np.random.rand(*q[i].shape)
                    if hasattr(obs, "mask"):
                        q_[~obs.mask[i]] = -np.inf
                    act[i] = q_.argmax()
        return Batch(logits=dist, act=act, state=h)

    def _target_dist(self, batch: Batch) -> torch.Tensor:
        if self._target:
            a = self(batch, input="obs_next").act
            next_dist = self(
                batch, model="model_old", input="obs_next"
            ).logits
        else:
            next_b = self(batch, input="obs_next")
            a = next_b.act
            next_dist = next_b.logits
        next_dist = next_dist[np.arange(len(a)), a, :]
        target_support = batch.returns.clamp(self._v_min, self._v_max)
        # An amazing trick for calculating the projection gracefully.
        # ref: https://github.com/ShangtongZhang/DeepRL
        target_dist = (1 - (target_support.unsqueeze(1) -
                            self.support.view(1, -1, 1)).abs() / self.delta_z
                       ).clamp(0, 1) * next_dist.unsqueeze(1)
        return target_dist.sum(-1)

    def learn(self, batch: Batch, **kwargs: Any) -> Dict[str, float]:
        if self._target and self._cnt % self._freq == 0:
            self.sync_weight()
        self.optim.zero_grad()
        with torch.no_grad():
            target_dist = self._target_dist(batch)
        weight = batch.pop("weight", 1.0)
        curr_dist = self(batch).logits
        act = batch.act
        curr_dist = curr_dist[np.arange(len(act)), act, :]
        cross_entropy = - (target_dist * torch.log(curr_dist + 1e-8)).sum(1)
        loss = (cross_entropy * weight).mean()
        # ref: https://github.com/Kaixhin/Rainbow/blob/master/agent.py L94-100
        batch.weight = cross_entropy.detach()  # prio-buffer
        loss.backward()
        self.optim.step()
        self._cnt += 1
        return {"loss": loss.item()}
