"""Base Types for Environments."""

from dataclasses import dataclass
from typing import Any

import jax
from flax.core import FrozenDict
from jaxtyping import Array, PRNGKeyArray


@jax.tree_util.register_dataclass
@dataclass
class EnvState:
    """Base environment state class."""

    # Data attributes
    obs: FrozenDict[str, Array]
    reward: Array  # R(prev_state, action_at_prev_state, current_state)
    done: Array
    commands: FrozenDict[str, Array]

    # Auxiliary attributes
    time: Array
    rng: PRNGKeyArray
    action_at_prev_step: Array
    command_at_prev_step: FrozenDict[str, Array]
    action_log_prob_at_prev_step: Array
