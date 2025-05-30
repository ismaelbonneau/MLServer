import pytest
import os
import asyncio
from copy import deepcopy
from typing import Optional
from unittest.mock import patch

from mlserver.env import Environment, compute_hash_of_file
from mlserver.model import MLModel
from mlserver.settings import Settings, ModelSettings, ModelParameters
from mlserver.types import InferenceRequest
from mlserver.codecs import StringCodec
from mlserver.parallel.errors import EnvironmentNotFound
from mlserver.parallel.registry import (
    InferencePoolRegistry,
    _set_environment_hash,
    _get_environment_hash,
    _append_gid_environment_hash,
    ENV_HASH_ATTR,
)

from ..fixtures import SumModel, EnvModel


@pytest.fixture
async def env_model(
    inference_pool_registry: InferencePoolRegistry, env_model_settings: ModelSettings
) -> MLModel:
    env_model = EnvModel(env_model_settings)
    model = await inference_pool_registry.load_model(env_model)

    yield model

    await inference_pool_registry.unload_model(model)


@pytest.fixture
async def existing_env_model(
    inference_pool_registry: InferencePoolRegistry,
    existing_env_model_settings: ModelSettings,
) -> MLModel:
    env_model = EnvModel(existing_env_model_settings)
    model = await inference_pool_registry.load_model(env_model)

    yield model

    await inference_pool_registry.unload_model(model)


def test_set_environment_hash(sum_model: MLModel):
    env_hash = "0e46fce1decb7a89a8b91c71d8b6975630a17224d4f00094e02e1a732f8e95f3"
    _set_environment_hash(sum_model, env_hash)

    assert hasattr(sum_model, ENV_HASH_ATTR)
    assert getattr(sum_model, ENV_HASH_ATTR) == env_hash


@pytest.mark.parametrize(
    "env_hash",
    ["0e46fce1decb7a89a8b91c71d8b6975630a17224d4f00094e02e1a732f8e95f3", None],
)
def test_get_environment_hash(sum_model: MLModel, env_hash: str):
    if env_hash:
        _set_environment_hash(sum_model, env_hash)

    assert _get_environment_hash(sum_model) == env_hash


async def test_default_pool(
    inference_pool_registry: InferencePoolRegistry, settings: Settings
):
    assert inference_pool_registry._default_pool is not None

    worker_count = len(inference_pool_registry._default_pool._workers)
    assert worker_count == settings.parallel_workers


@pytest.mark.parametrize("inference_pool_gid", ["dummy_id", None])
async def test_load_model(
    inference_pool_registry: InferencePoolRegistry,
    sum_model_settings: ModelSettings,
    inference_request: InferenceRequest,
    inference_pool_gid: str,
):
    sum_model_settings = deepcopy(sum_model_settings)
    sum_model_settings.name = "foo"
    sum_model_settings.parameters.inference_pool_gid = inference_pool_gid
    sum_model = SumModel(sum_model_settings)

    model = await inference_pool_registry.load_model(sum_model)
    inference_response = await model.predict(inference_request)

    assert inference_response.id == inference_request.id
    assert inference_response.model_name == sum_model.settings.name
    assert len(inference_response.outputs) == 1

    await inference_pool_registry.unload_model(sum_model)


def check_sklearn_version(response):
    # Note: These versions come from the `environment.yml` found in
    # `./tests/testdata/environment.yaml`
    assert len(response.outputs) == 1
    assert response.outputs[0].name == "sklearn_version"
    [sklearn_version] = StringCodec.decode_output(response.outputs[0])
    assert sklearn_version == "1.3.1"


async def test_load_model_with_env(
    inference_pool_registry: InferencePoolRegistry,
    env_model: MLModel,
    inference_request: InferenceRequest,
):
    response = await env_model.predict(inference_request)
    check_sklearn_version(response)


async def test_load_model_with_existing_env(
    inference_pool_registry: InferencePoolRegistry,
    existing_env_model: MLModel,
    inference_request: InferenceRequest,
):
    response = await existing_env_model.predict(inference_request)
    check_sklearn_version(response)


async def test_load_creates_pool(
    inference_pool_registry: InferencePoolRegistry,
    env_model_settings: MLModel,
):
    assert len(inference_pool_registry._pools) == 0
    env_model = EnvModel(env_model_settings)
    await inference_pool_registry.load_model(env_model)

    assert len(inference_pool_registry._pools) == 1


async def test_load_reuses_pool(
    inference_pool_registry: InferencePoolRegistry,
    env_model: MLModel,
    env_model_settings: ModelSettings,
):
    env_model_settings.name = "foo"
    new_model = EnvModel(env_model_settings)

    assert len(inference_pool_registry._pools) == 1
    await inference_pool_registry.load_model(new_model)

    assert len(inference_pool_registry._pools) == 1


async def test_load_reuses_env_folder(
    inference_pool_registry: InferencePoolRegistry,
    env_model_settings: ModelSettings,
    env_tarball: str,
):
    env_model_settings.name = "foo"
    new_model = EnvModel(env_model_settings)

    # Make sure there's already existing env
    env_hash = await compute_hash_of_file(env_tarball)
    env_path = inference_pool_registry._get_env_path(env_hash)
    await Environment.from_tarball(env_tarball, env_path, env_hash)

    await inference_pool_registry.load_model(new_model)


async def test_reload_model_with_env(
    inference_pool_registry: InferencePoolRegistry,
    env_model: MLModel,
    env_model_settings: ModelSettings,
):
    env_model_settings.parameters.version = "v2.0"
    new_model = EnvModel(env_model_settings)

    assert len(inference_pool_registry._pools) == 1
    await inference_pool_registry.reload_model(env_model, new_model)

    assert len(inference_pool_registry._pools) == 1


async def test_unload_model_removes_pool_if_empty(
    inference_pool_registry: InferencePoolRegistry,
    env_model_settings: MLModel,
):
    env_model = EnvModel(env_model_settings)
    assert len(inference_pool_registry._pools) == 0

    model = await inference_pool_registry.load_model(env_model)
    assert len(inference_pool_registry._pools) == 1

    await inference_pool_registry.unload_model(model)

    env_hash = _get_environment_hash(model)
    env_path = inference_pool_registry._get_env_path(env_hash)
    assert len(inference_pool_registry._pools) == 0
    assert not os.path.isdir(env_path)


async def test_invalid_env_hash(
    inference_pool_registry: InferencePoolRegistry, sum_model: MLModel
):
    _set_environment_hash(sum_model, "foo")
    with pytest.raises(EnvironmentNotFound):
        await inference_pool_registry._find(sum_model)


async def test_worker_stop(
    settings: Settings,
    inference_pool_registry: InferencePoolRegistry,
    sum_model: MLModel,
    inference_request: InferenceRequest,
    caplog,
):
    # Pick random worker and kill it
    default_pool = inference_pool_registry._default_pool
    workers = list(default_pool._workers.values())
    stopped_worker = workers[0]
    stopped_worker.kill()

    # Give some time for worker to come up
    await asyncio.sleep(5)

    # Ensure SIGCHD signal was handled
    assert f"with PID {stopped_worker.pid}" in caplog.text

    # Cycle through every worker
    assert len(default_pool._workers) == settings.parallel_workers
    for _ in range(settings.parallel_workers + 2):
        inference_response = await sum_model.predict(inference_request)
        assert len(inference_response.outputs) > 0


@pytest.mark.parametrize(
    "env_hash, inference_pool_gid, expected_env_hash",
    [
        ("dummy_hash", "dummy_gid", "dummy_hash-dummy_gid"),
    ],
)
async def test__get_environment_hash_gid(
    env_hash: str, inference_pool_gid: Optional[str], expected_env_hash: str
):
    _env_hash = _append_gid_environment_hash(env_hash, inference_pool_gid)
    assert _env_hash == expected_env_hash


async def test_default_and_default_gid(
    inference_pool_registry: InferencePoolRegistry,
    simple_model_settings: ModelSettings,
):
    simple_model_settings_gid = deepcopy(simple_model_settings)
    simple_model_settings_gid.parameters.inference_pool_gid = "dummy_id"

    simple_model = SumModel(simple_model_settings)
    simple_model_gid = SumModel(simple_model_settings_gid)

    model = await inference_pool_registry.load_model(simple_model)
    model_gid = await inference_pool_registry.load_model(simple_model_gid)

    assert len(inference_pool_registry._pools) == 1
    await inference_pool_registry.unload_model(model)
    await inference_pool_registry.unload_model(model_gid)


async def test_env_and_env_gid(
    inference_request: InferenceRequest,
    inference_pool_registry: InferencePoolRegistry,
    env_model_settings: ModelSettings,
    env_tarball: str,
):
    env_model_settings = deepcopy(env_model_settings)
    env_model_settings.parameters.environment_tarball = env_tarball

    env_model_settings_gid = deepcopy(env_model_settings)
    env_model_settings_gid.parameters.inference_pool_gid = "dummy_id"

    env_model = EnvModel(env_model_settings)
    env_model_gid = EnvModel(env_model_settings_gid)

    model = await inference_pool_registry.load_model(env_model)
    model_gid = await inference_pool_registry.load_model(env_model_gid)
    assert len(inference_pool_registry._pools) == 2

    response = await model.predict(inference_request)
    response_gid = await model_gid.predict(inference_request)
    check_sklearn_version(response)
    check_sklearn_version(response_gid)

    await inference_pool_registry.unload_model(model)
    await inference_pool_registry.unload_model(model_gid)


@pytest.mark.parametrize(
    "inference_pool_grid, autogenerate_inference_pool_grid",
    [
        ("dummy_gid", False),
        ("dummy_gid", True),
        (None, True),
        (None, False),
    ],
)
def test_autogenerate_inference_pool_gid(
    inference_pool_grid: str, autogenerate_inference_pool_grid: bool
):
    patch_uuid = "patch-uuid"
    with patch("uuid.uuid4", return_value=patch_uuid):
        model_settings = ModelSettings(
            name="dummy-model",
            implementation=MLModel,
            parameters=ModelParameters(
                inference_pool_gid=inference_pool_grid,
                autogenerate_inference_pool_gid=autogenerate_inference_pool_grid,
            ),
        )

    expected_gid = (
        inference_pool_grid
        if not autogenerate_inference_pool_grid
        else (inference_pool_grid or patch_uuid)
    )
    assert model_settings.parameters.inference_pool_gid == expected_gid
