#  Copyright 2023 Red Hat, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import base64
import json
import logging
import os

from dateutil import parser
from django.conf import settings
from podman import PodmanClient
from podman.domain.images import Image
from podman.errors import ContainerError, ImageNotFound
from podman.errors.exceptions import APIError, NotFound

from aap_eda.core.enums import ActivationStatus

from . import exceptions, messages
from .common import (
    ContainerEngine,
    ContainerRequest,
    ContainerStatus,
    Credential,
    LogHandler,
)

LOGGER = logging.getLogger(__name__)


def get_podman_client() -> PodmanClient:
    """Podman client factory."""
    try:
        podman_url = settings.PODMAN_SOCKET_URL
        if podman_url:
            return PodmanClient(base_url=podman_url)

        if os.getuid() == 0:
            podman_url = "unix:///run/podman/podman.sock"
        else:
            xdg_runtime_dir = os.getenv(
                "XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"
            )
            podman_url = f"unix://{xdg_runtime_dir}/podman/podman.sock"
        LOGGER.info(f"Using podman socket: {podman_url}")
        return PodmanClient(base_url=podman_url)
    except ValueError as e:
        LOGGER.error(f"Failed to initialize podman client: f{e}")
        raise exceptions.ContainerEngineInitError(str(e))


class Engine(ContainerEngine):
    def __init__(
        self,
        _activation_id: str,
        client=None,
    ) -> None:
        try:
            if client:
                self.client = client
            else:
                self.client = get_podman_client()
            LOGGER.debug(self.client.version())

            self.auth_file = None
        except APIError as e:
            LOGGER.error(f"Failed to initialize podman engine: f{e}")
            raise exceptions.ContainerEngineInitError(str(e))

    def cleanup(self, container_id: str, log_handler: LogHandler) -> None:
        try:
            if self.client.containers.exists(container_id):
                container = self.client.containers.get(container_id)
                try:
                    container.stop(ignore=True)
                    LOGGER.info(f"Container {container_id} is cleaned up.")
                    self.update_logs(container_id, log_handler)
                    self._cleanup(container_id, log_handler)
                    log_handler.write(
                        f"Container {container_id} is cleaned up.",
                        flush=True,
                    )
                except NotFound:
                    LOGGER.info(f"Container {container_id} not found.")
                    log_handler.write(
                        f"Container {container_id} not found.",
                        flush=True,
                    )
        # ContainerCleanupError handled by the manager
        except APIError as e:
            raise exceptions.ContainerCleanupError(str(e)) from e

    def _image_exists(self, image_url: str) -> bool:
        try:
            self.client.images.get(image_url)
        except ImageNotFound:
            return False
        return True

    def start(self, request: ContainerRequest, log_handler: LogHandler) -> str:
        if not request.image_url:
            raise exceptions.ContainerStartError("Missing image url")

        try:
            self._set_auth_json_file()
            self._login(request)
            LOGGER.info(f"Image URL is {request.image_url}")
            if request.pull_policy == "Always" or not self._image_exists(
                request.image_url,
            ):
                self._pull_image(request, log_handler)

            log_handler.write("Starting Container", True)
            command = request.cmdline.command_and_args()
            log_handler.write(f"Container args {command}", True)
            pod_args = self._load_pod_args(request)
            LOGGER.info(
                "Creating container: "
                f"command: {command}, "
                f"pod_args: {pod_args}"
            )
            container = self.client.containers.run(
                image=request.image_url,
                command=command,
                stdout=True,
                stderr=True,
                remove=True,
                detach=True,
                **pod_args,
            )

            LOGGER.info(
                f"Created container: "
                f"name: {container.name}, "
                f"id: {container.id}, "
                f"ports: {container.ports}, "
                f"status: {container.status}, "
                f"command: {command}, "
                f"pod_args: {pod_args}"
            )

            log_handler.write(f"Container {container.id} is started.", True)
            return str(container.id)
        except (
            ContainerError,
            ImageNotFound,
            APIError,
        ) as e:
            error_message = f"Container Start Error: {e}"
            LOGGER.error(error_message)
            log_handler.write(error_message, flush=True)
            raise exceptions.ContainerStartError(error_message) from e

    def get_status(self, container_id: str) -> ContainerStatus:
        if not self.client.containers.exists(container_id):
            raise exceptions.ContainerNotFoundError(
                f"Container id {container_id} not found"
            )

        container = self.client.containers.get(container_id)
        error_msg = container.attrs.get("State").get("Error", "")

        # Check container status
        # Ref: https://github.com/containers/podman/blob/main/libpod/define/containerstate.go # noqa: E501
        if container.status in ["exited", "stopped"]:
            exit_code = container.attrs.get("State").get("ExitCode")
            if exit_code == 0:
                message = messages.POD_COMPLETED.format(
                    pod_id=container_id,
                )
                return ContainerStatus(
                    status=ActivationStatus.COMPLETED,
                    message=message,
                )
            if not error_msg:
                error_msg = messages.POD_GENERIC_FAIL.format(
                    pod_id=container_id,
                    exit_code=exit_code,
                )
            return ContainerStatus(
                status=ActivationStatus.FAILED,
                message=error_msg,
            )

        if container.status in ["running", "stopping"]:
            return ContainerStatus(
                status=ActivationStatus.RUNNING,
                message=messages.POD_RUNNING.format(
                    pod_id=container_id,
                ),
            )

        if container.status == "created":
            if not error_msg:
                error_msg = messages.POD_NOT_RUNNING.format(
                    pod_id=container_id,
                )

            return ContainerStatus(
                status=ActivationStatus.FAILED,
                message=error_msg,
            )

        # Not expected statuses
        if container.status in [
            "paused",
            "restarting",
            "removing",
            "dead",
            "configured",
            "unknown",
        ]:
            return ContainerStatus(
                status=ActivationStatus.FAILED,
                message=messages.POD_WRONG_STATE.format(
                    pod_id=container_id,
                    pod_state=container.status,
                ),
            )

        # undocumented status, fail safe
        return ContainerStatus(
            status=ActivationStatus.ERROR,
            message=messages.POD_UNEXPECTED.format(
                pod_id=container_id,
                pod_state=container.status,
            ),
        )

    def update_logs(self, container_id: str, log_handler: LogHandler) -> None:
        try:
            if not self.client.containers.exists(container_id):
                LOGGER.warning(f"Container {container_id} not found.")
                log_handler.write(f"Container {container_id} not found.", True)
                return

            since = None
            log_read_at = log_handler.get_log_read_at()
            if log_read_at:
                since = int(log_handler.get_log_read_at().timestamp()) + 1

            container = self.client.containers.get(container_id)
            log_args = {"timestamps": True, "stderr": True}
            if since:
                log_args["since"] = since
            timestamp = None
            for logline in container.logs(**log_args):
                log = logline.decode("utf-8").strip()
                log_parts = log.split(" ", 1)
                timestamp = log_parts[0]
                if len(log_parts) > 1:
                    log_handler.write(
                        lines=log_parts[1], flush=False, timestamp=False
                    )

            if timestamp:
                dt = parser.parse(timestamp)
                log_handler.flush()
                log_handler.set_log_read_at(dt)

        # ContainerUpdateLogsError handled by the manager
        except APIError as e:
            raise exceptions.ContainerUpdateLogsError(str(e)) from e

    def _cleanup(self, container_id: str, _log_handler: LogHandler) -> None:
        try:
            if self.client.containers.exists(container_id):
                container = self.client.containers.get(container_id)
                try:
                    container.remove(force=True, v=True)
                    LOGGER.info(f"Container {container_id} is cleaned up.")
                except NotFound:
                    LOGGER.info(f"Container {container_id} not found.")
        except APIError as e:
            LOGGER.error(f"Failed to cleanup {container_id}: {e}")
            raise exceptions.ContainerCleanupError(str(e))

    def _get_ports(self, found_ports: list[tuple]) -> dict:
        ports = {}
        for _, port in found_ports:
            ports[f"{port}/tcp"] = port

        return ports

    def _login(self, request: ContainerRequest) -> None:
        credential = request.credential
        if not credential:
            return

        try:
            registry = request.image_url.split("/")[0]
            self.client.login(
                username=credential.username,
                password=credential.secret,
                registry=registry,
            )

            LOGGER.debug(
                f"{credential.username} login succeeded to {registry}"
            )
        except APIError as e:
            LOGGER.exception("Login failed: f{e}")
            raise exceptions.ContainerStartError(str(e))

    def _write_auth_json(self, request: ContainerRequest) -> None:
        if not self.auth_file:
            LOGGER.debug("No auth file to create")
            return

        auth_dict = {}
        if os.path.exists(self.auth_file):
            with open(self.auth_file, encoding="utf-8") as f:
                auth_dict = json.load(f)

        if "auths" not in auth_dict:
            auth_dict["auths"] = {}
        registry = request.image_url.split("/")[0]
        auth_dict["auths"][registry] = self._create_auth_key(
            request.credential
        )

        with open(self.auth_file, mode="w", encoding="utf-8") as f:
            json.dump(auth_dict, f, indent=6)

    def _create_auth_key(self, credential: Credential) -> dict:
        data = f"{credential.username}:{credential.secret}"
        encoded_data = data.encode("ascii")
        return {"auth": base64.b64encode(encoded_data).decode("ascii")}

    def _set_auth_json_file(self) -> None:
        xdg_runtime_dir = os.getenv(
            "XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"
        )
        auth_file = f"{xdg_runtime_dir}/containers/auth.json"
        dir_name = os.path.dirname(auth_file)
        if os.path.exists(dir_name):
            self.auth_file = auth_file
            LOGGER.debug("Will use auth file %s", auth_file)
        else:
            self.auth_file = None
            LOGGER.debug("Will not use auth file")

    def _pull_image(
        self, request: ContainerRequest, log_handler: LogHandler
    ) -> Image:
        try:
            log_handler.write(f"Pulling image {request.image_url}", True)
            LOGGER.info(f"Pulling image : {request.image_url}")
            kwargs = {}
            if request.credential:
                kwargs["auth_config"] = {
                    "username": request.credential.username,
                    "password": request.credential.secret,
                }
                self._write_auth_json(request)
            image = self.client.images.pull(request.image_url, **kwargs)

            # https://github.com/containers/podman-py/issues/301
            if not image.id:
                msg = messages.IMAGE_PULL_ERROR.format(
                    image_url=request.image_url,
                )
                LOGGER.error(msg)
                log_handler.write(msg, True)
                raise exceptions.ContainerImagePullError(msg)
            LOGGER.info("Downloaded image")
            return image
        except ImageNotFound as e:
            msg = f"Image {request.image_url} not found"
            LOGGER.exception(msg)
            log_handler.write(msg, True)
            raise exceptions.ContainerImagePullError(msg) from e
        except APIError as e:
            LOGGER.exception("Failed to pull image {request.image_url}: f{e}")
            raise exceptions.ContainerStartError(str(e))

    def _load_pod_args(self, request: ContainerRequest) -> dict:
        pod_args = {"name": request.name}
        if request.ports:
            pod_args["ports"] = self._get_ports(request.ports)

        if request.mem_limit:
            pod_args["mem_limit"] = request.mem_limit

        if request.mounts:
            pod_args["mounts"] = request.mounts

        if request.env_vars:
            pod_args["environment"] = request.env_vars

        if request.extra_args:
            for key, value in request.extra_args.items():
                pod_args[key] = value

        for key, value in pod_args.items():
            LOGGER.debug("Key %s Value %s", key, value)

        LOGGER.info(pod_args)
        return pod_args
