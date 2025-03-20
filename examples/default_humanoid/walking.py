"""Defines simple task for training a walking policy for K-Bot."""

from dataclasses import dataclass
from pathlib import Path

import attrs
import distrax
import equinox as eqx
import jax
import jax.numpy as jnp
import mujoco
import optax
import xax
from flax.core import FrozenDict
from jaxtyping import Array, PRNGKeyArray
from kscale.web.gen.api import JointMetadataOutput
from mujoco import mjx

from ksim.actuators import Actuators, MITPositionActuators, TorqueActuators
from ksim.commands import Command, LinearVelocityCommand
from ksim.env.data import PhysicsModel, Trajectory
from ksim.observation import ActuatorForceObservation, Observation
from ksim.randomization import (
    Randomization,
    WeightRandomization,
)
from ksim.resets import RandomJointPositionReset, RandomJointVelocityReset, Reset
from ksim.rewards import (
    AngularVelocityXYPenalty,
    JointVelocityPenalty,
    LinearVelocityZPenalty,
    Reward,
    TerminationPenalty,
)
from ksim.task.ppo import PPOConfig, PPOTask
from ksim.terminations import BadZTermination, FastAccelerationTermination, Termination
from ksim.utils.mujoco import get_joint_metadata

OBS_SIZE = 27
CMD_SIZE = 2
NUM_INPUTS = OBS_SIZE + CMD_SIZE
NUM_OUTPUTS = 21


@attrs.define(frozen=True, kw_only=True)
class DHForwardReward(Reward):
    """Incentives forward movement."""

    def __call__(self, trajectory: Trajectory) -> Array:
        # Take just the x velocity component
        x_delta = -jnp.clip(trajectory.qvel[..., 1], -1.0, 1.0)
        return x_delta


@attrs.define(frozen=True, kw_only=True)
class DHControlPenalty(Reward):
    """Legacy default humanoid control cost that penalizes squared action magnitude."""

    def __call__(self, trajectory: Trajectory) -> Array:
        return jnp.sum(jnp.square(trajectory.action), axis=-1)


class DefaultHumanoidActor(eqx.Module):
    """Actor for the walking task."""

    mlp: eqx.nn.MLP
    min_std: float = eqx.static_field()
    max_std: float = eqx.static_field()
    var_scale: float = eqx.static_field()
    mean_scale: float = eqx.static_field()

    def __init__(
        self,
        key: PRNGKeyArray,
        *,
        min_std: float,
        max_std: float,
        var_scale: float,
        mean_scale: float,
    ) -> None:
        self.mlp = eqx.nn.MLP(
            in_size=NUM_INPUTS,
            out_size=NUM_OUTPUTS * 2,
            width_size=64,
            depth=5,
            key=key,
            activation=jax.nn.relu,
        )
        self.min_std = min_std
        self.max_std = max_std
        self.var_scale = var_scale
        self.mean_scale = mean_scale

    def __call__(
        self,
        act_frc_obs_n: Array,
        lin_vel_cmd_n: Array,
    ) -> distrax.Normal:
        x_n = jnp.concatenate([act_frc_obs_n, lin_vel_cmd_n], axis=-1)  # (NUM_INPUTS)

        # Split the output into mean and standard deviation.
        prediction_n = self.mlp(x_n)
        mean_n = prediction_n[..., :NUM_OUTPUTS]
        std_n = prediction_n[..., NUM_OUTPUTS:]

        # Scale the mean.
        mean_n = jnp.tanh(mean_n) * self.mean_scale

        # Softplus and clip to ensure positive standard deviations.
        std_n = (jax.nn.softplus(std_n) + self.min_std) * self.var_scale
        std_n = jnp.clip(std_n, self.min_std, self.max_std)

        # return distrax.Transformed(distrax.Normal(mean_n, std_n), distrax.Tanh())
        return distrax.Normal(mean_n, std_n)


class DefaultHumanoidCritic(eqx.Module):
    """Critic for the walking task."""

    mlp: eqx.nn.MLP

    def __init__(self, key: PRNGKeyArray) -> None:
        self.mlp = eqx.nn.MLP(
            in_size=NUM_INPUTS,
            out_size=1,  # Always output a single critic value.
            width_size=64,
            depth=5,
            key=key,
            activation=jax.nn.relu,
        )

    def __call__(
        self,
        act_frc_obs_n: Array,
        lin_vel_cmd_n: Array,
    ) -> Array:
        x_n = jnp.concatenate([act_frc_obs_n, lin_vel_cmd_n], axis=-1)  # (NUM_INPUTS)
        return self.mlp(x_n)


class DefaultHumanoidModel(eqx.Module):
    actor: DefaultHumanoidActor
    critic: DefaultHumanoidCritic

    def __init__(self, key: PRNGKeyArray) -> None:
        self.actor = DefaultHumanoidActor(
            key,
            min_std=0.01,
            max_std=1.0,
            var_scale=1.0,
            mean_scale=1.0,
        )
        self.critic = DefaultHumanoidCritic(key)


@dataclass
class HumanoidWalkingTaskConfig(PPOConfig):
    """Config for the humanoid walking task."""

    # Optimizer parameters.
    learning_rate: float = xax.field(
        value=1e-4,
        help="Learning rate for PPO.",
    )
    max_grad_norm: float = xax.field(
        value=0.5,
        help="Maximum gradient norm for clipping.",
    )
    adam_weight_decay: float = xax.field(
        value=0.0,
        help="Weight decay for the Adam optimizer.",
    )

    # Mujoco parameters.
    use_mit_actuators: bool = xax.field(
        value=True,
        help="Whether to use the MIT actuator model, where the actions are position commands",
    )
    kp: float = xax.field(
        value=1.0,
        help="The Kp for the actuators",
    )
    kd: float = xax.field(
        value=0.1,
        help="The Kd for the actuators",
    )
    armature: float = xax.field(
        value=1e-2,
        help="A value representing the effective inertia of the actuator armature",
    )
    friction: float = xax.field(
        value=1e-6,
        help="The dynamic friction loss for the actuator",
    )

    # Rendering parameters.
    render_track_body_id: int | None = xax.field(
        value=0,
        help="The body id to track with the render camera.",
    )

    # Checkpointing parameters.
    export_for_inference: bool = xax.field(
        value=False,
        help="Whether to export the model for inference.",
    )


class HumanoidWalkingTask(PPOTask[HumanoidWalkingTaskConfig]):
    def get_optimizer(self) -> optax.GradientTransformation:
        """Builds the optimizer.

        This provides a reasonable default optimizer for training PPO models,
        but can be overridden by subclasses who want to do something different.
        """
        optimizer = optax.chain(
            optax.clip_by_global_norm(self.config.max_grad_norm),
            (
                optax.adam(self.config.learning_rate)
                if self.config.adam_weight_decay == 0.0
                else optax.adamw(self.config.learning_rate, weight_decay=self.config.adam_weight_decay)
            ),
        )

        return optimizer

    def get_mujoco_model(self) -> tuple[mujoco.MjModel, dict[str, JointMetadataOutput]]:
        mjcf_path = (Path(__file__).parent / "scene.mjcf").resolve().as_posix()
        mj_model = mujoco.MjModel.from_xml_path(mjcf_path)

        mj_model.opt.timestep = jnp.array(self.config.dt)
        mj_model.opt.iterations = 6
        mj_model.opt.ls_iterations = 6
        mj_model.opt.disableflags = mjx.DisableBit.EULERDAMP
        mj_model.opt.solver = mjx.SolverType.CG

        return mj_model

    def get_mujoco_model_metadata(self, mj_model: mujoco.MjModel) -> dict[str, JointMetadataOutput]:
        return get_joint_metadata(
            mj_model,
            kp=self.config.kp,
            kd=self.config.kd,
            armature=self.config.armature,
            friction=self.config.friction,
        )

    def get_actuators(self, physics_model: PhysicsModel, metadata: dict[str, JointMetadataOutput]) -> Actuators:
        if self.config.use_mit_actuators:
            return MITPositionActuators(physics_model, metadata)
        else:
            return TorqueActuators()

    def get_randomization(self, physics_model: PhysicsModel) -> list[Randomization]:
        return [
            WeightRandomization(scale=0.01),
        ]

    def get_resets(self, physics_model: PhysicsModel) -> list[Reset]:
        return [
            RandomJointPositionReset(scale=0.01),
            RandomJointVelocityReset(scale=0.01),
        ]

    def get_observations(self, physics_model: PhysicsModel) -> list[Observation]:
        return [
            ActuatorForceObservation(),
        ]

    def get_commands(self, physics_model: PhysicsModel) -> list[Command]:
        return [
            LinearVelocityCommand(x_scale=0.0, y_scale=0.0, switch_prob=0.02, zero_prob=0.3),
        ]

    def get_rewards(self, physics_model: PhysicsModel) -> list[Reward]:
        return [
            DHForwardReward(scale=0.2),
            DHControlPenalty(scale=-0.01),
            TerminationPenalty(scale=-1.0),
            JointVelocityPenalty(scale=-0.01),
            # These seem necessary to prevent some physics artifacts.
            LinearVelocityZPenalty(scale=-0.001),
            AngularVelocityXYPenalty(scale=-0.001),
        ]

    def get_terminations(self, physics_model: PhysicsModel) -> list[Termination]:
        return [
            BadZTermination(unhealthy_z_lower=0.8, unhealthy_z_upper=4.0),
            FastAccelerationTermination(),
        ]

    def get_model(self, key: PRNGKeyArray) -> DefaultHumanoidModel:
        return DefaultHumanoidModel(key)

    def get_initial_carry(self) -> None:
        return None

    def _run_actor(
        self,
        model: DefaultHumanoidModel,
        observations: FrozenDict[str, Array],
        commands: FrozenDict[str, Array],
    ) -> distrax.Normal:
        act_frc_obs_n = observations["actuator_force_observation"] / 100.0
        lin_vel_cmd_n = commands["linear_velocity_command"]
        return model.actor(act_frc_obs_n, lin_vel_cmd_n)

    def _run_critic(
        self,
        model: DefaultHumanoidModel,
        observations: FrozenDict[str, Array],
        commands: FrozenDict[str, Array],
    ) -> Array:
        act_frc_obs_n = observations["actuator_force_observation"] / 100.0
        lin_vel_cmd_n = commands["linear_velocity_command"]
        return model.critic(act_frc_obs_n, lin_vel_cmd_n)

    def get_on_policy_log_probs(
        self,
        model: DefaultHumanoidModel,
        trajectories: Trajectory,
        rng: PRNGKeyArray,
    ) -> Array:
        log_probs, _ = trajectories.aux_outputs
        return log_probs

    def get_on_policy_values(
        self,
        model: DefaultHumanoidModel,
        trajectories: Trajectory,
        rng: PRNGKeyArray,
    ) -> Array:
        _, values = trajectories.aux_outputs
        return values

    def get_log_probs(
        self,
        model: DefaultHumanoidModel,
        trajectories: Trajectory,
        rng: PRNGKeyArray,
    ) -> tuple[Array, Array]:
        # Vectorize over both batch and time dimensions.
        time_par_fn = jax.vmap(self._run_actor, in_axes=(None, 0, 0))
        batch_par_fn = jax.vmap(time_par_fn, in_axes=(None, 0, 0))
        action_dist_btn = batch_par_fn(model, trajectories.obs, trajectories.command)

        # Compute the log probabilities of the trajectory's actions according
        # to the current policy, along with the entropy of the distribution.
        action_btn = trajectories.action
        log_probs_btn = action_dist_btn.log_prob(action_btn)
        entropy_btn = action_dist_btn.entropy()

        return log_probs_btn, entropy_btn

    def get_values(
        self,
        model: DefaultHumanoidModel,
        trajectories: Trajectory,
        rng: PRNGKeyArray,
    ) -> Array:
        # Vectorize over both batch and time dimensions.
        time_par_fn = jax.vmap(self._run_critic, in_axes=(None, 0, 0))
        batch_par_fn = jax.vmap(time_par_fn, in_axes=(None, 0, 0))
        values_bt1 = batch_par_fn(model, trajectories.obs, trajectories.command)

        # Remove the last dimension.
        return values_bt1.squeeze(-1)

    def sample_action(
        self,
        model: DefaultHumanoidModel,
        carry: None,
        physics_model: PhysicsModel,
        observations: FrozenDict[str, Array],
        commands: FrozenDict[str, Array],
        rng: PRNGKeyArray,
    ) -> tuple[Array, None, tuple[Array, Array]]:
        action_dist_n = self._run_actor(model, observations, commands)
        action_n = action_dist_n.sample(seed=rng)
        action_log_prob_n = action_dist_n.log_prob(action_n)

        critic_n = self._run_critic(model, observations, commands)
        value_n = critic_n.squeeze(-1)

        return action_n, None, (action_log_prob_n, value_n)

    def on_after_checkpoint_save(self, ckpt_path: Path, state: xax.State) -> xax.State:
        state = super().on_after_checkpoint_save(ckpt_path, state)

        if not self.config.export_for_inference:
            return state

        # Load the checkpoint and export it using xax's export function.
        model: DefaultHumanoidModel = self.load_checkpoint(ckpt_path, part="model")

        def model_fn(obs: Array, cmd: Array) -> Array:
            return model.actor(obs, cmd).mode()

        input_shapes = [(OBS_SIZE,), (CMD_SIZE,)]
        xax.export(model_fn, input_shapes, ckpt_path.parent / "tf_model")

        return state


if __name__ == "__main__":
    # python -m examples.default_humanoid.walking run_environment=True
    HumanoidWalkingTask.launch(
        HumanoidWalkingTaskConfig(
            # Update parameters. These values are very small, which is useful
            # for testing on your local machine.
            num_envs=8,
            batch_size=32,
            # Simulation parameters.
            dt=0.005,
            ctrl_dt=0.02,
            max_action_latency=0.0,
            min_action_latency=0.0,
            rollout_length_seconds=20.0,
            eval_rollout_length_seconds=5.0,
            # PPO parameters
            gamma=0.97,
            lam=0.95,
            entropy_coef=0.001,
            clip_param=0.3,
        ),
    )
