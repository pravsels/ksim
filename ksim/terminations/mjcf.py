"""Defines some useful termination conditions for MJCF environments."""

from typing import Collection

import jax.numpy as jnp
from brax.base import State
from brax.mjx.base import State as MjxState

from ksim.terminations.base import Termination, TerminationBuilder
from ksim.utils.data import BuilderData
from ksim.utils.mujoco import link_names_to_ids


class PitchTooGreatTermination(Termination):
    """Terminates the episode if the pitch is too great."""

    max_pitch: float

    def __init__(self, max_pitch: float) -> None:
        super().__init__()

        self.max_pitch = max_pitch

    def __call__(self, state: State) -> jnp.ndarray:
        quat = state.q[3:7]
        pitch = jnp.arctan2(2 * quat[1] * quat[2] - 2 * quat[0] * quat[3], 1 - 2 * quat[1] ** 2 - 2 * quat[2] ** 2)
        return jnp.abs(pitch) > self.max_pitch


class RollTooGreatTermination(Termination):
    """Terminates the episode if the roll is too great."""

    max_roll: float

    def __init__(self, max_roll: float) -> None:
        super().__init__()

        self.max_roll = max_roll

    def __call__(self, state: State) -> jnp.ndarray:
        quat = state.q[3:7]
        roll = jnp.arctan2(2 * quat[1] * quat[2] + 2 * quat[0] * quat[3], 1 - 2 * quat[2] ** 2 - 2 * quat[3] ** 2)
        return jnp.abs(roll) > self.max_roll


class MinimumHeightTermination(Termination):
    """Terminates the episode if the robot is too low."""

    min_height: float

    def __init__(self, min_height: float) -> None:
        super().__init__()

        self.min_height = min_height

    def __call__(self, state: State) -> jnp.ndarray:
        return state.q[2] < self.min_height


class IllegalContactTermination(Termination):
    """Terminates the episode if the robot is in an illegal contact."""

    link_ids: jnp.ndarray
    contact_eps: float

    def __init__(self, link_ids: Collection[int], contact_eps: float = -1e-3) -> None:
        super().__init__()

        self.link_ids = jnp.array(sorted(link_ids))
        self.contact_eps = contact_eps

    def __call__(self, state: State) -> jnp.ndarray:
        if state.contact is None:
            return jnp.zeros_like(state.q[0])

        contact = state.contact

        if isinstance(state, MjxState):
            has_contact = jnp.any(contact.geom[:, :, None] == self.link_ids[None, None, :], axis=(1, 2))
            return jnp.where(has_contact, contact.dist, 1e4).min() <= self.contact_eps

        else:
            raise NotImplementedError(f"IllegalContactTermination is not implemented for {type(state)}")


class IllegalContactTerminationBuilder(TerminationBuilder[IllegalContactTermination]):
    def __init__(self, link_names: Collection[str]) -> None:
        super().__init__()

        self.link_names = link_names

    def __call__(self, data: BuilderData) -> IllegalContactTermination:
        link_ids = link_names_to_ids(self.link_names, data.model)
        return IllegalContactTermination(link_ids)
