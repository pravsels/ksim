"""MJX Utils"""

from dataclasses import dataclass
from typing import Any
from jaxtyping import Array
from ksim.env.base_env import EnvState
from mujoco import mjx


@dataclass
class MjxEnvState(EnvState):
    """The state of the environment.

    Attributes (inheriteds):
        model: Handles physics and model definition (latter shouldn't be touched).
        data: Includes current state of the robot.
        obs: The post-processed observations of the environment.
        reward: The reward of the environment.
        done: Whether the episode is done.
        info: Additional information about the environment.
    """

    mjx_model: mjx.Model
    mjx_data: mjx.Data  # making this non-optional.
    obs: dict[str, Array]
    reward: Array
    done: Array
    info: dict[str, Any]
