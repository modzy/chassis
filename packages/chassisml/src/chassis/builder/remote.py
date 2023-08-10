from __future__ import annotations

import json
import os.path
import shutil
import tempfile
import time
import urllib.parse
import warnings
from .buildable import Buildable
from .options import BuildOptions, DefaultBuildOptions
import requests
import validators
from .utils import sanitize_image_name
from packaging import version

from .response import BuildResponse


class RemoteBuilder:
    def __init__(self, url: str, package: Buildable, options: BuildOptions = DefaultBuildOptions, credentials: str = None, tls_verify: bool = True):
        """
        Initializes a connection to a Chassis remote build server for a `Buildable` object (like `ChassisModel`).

        A Docker context also be prepared according to the options supplied. The Docker context is
        a directory (in `/tmp` unless `base_dir` is given in `options`) containing a Dockerfile
        and all the resources necessary to build the container. For more information on how the
        context is prepared given the supplied options, see `chassis.builder.Buildable.prepare_context`.

        Args:
            url (str): The URL to the Chassis remote build server. Example: "https://chassis.example.com:8443"
            package (Buildable): ChassisModel object that contains the code to be containerized.
            options (BuildOptions): Object that provides specific build configuration options. See `chassis.builder.BuildOptions` for more details.
            credentials (str): A string that will be used in the "Authorization" header. Default = None.
            tls_verify (bool): Whether to enable TLS verification. Default = True.

        Raises:
            ValueError if:
                - the URL is not valid
                - the build server is not available
                - the build server is too old

        Examples:
            See `RemoteBuilder.build_image`.
        """
        if not validators.url(url):
            raise ValueError("URL is not valid")
        self.url = url
        self.credentials = credentials
        self.tls_verify = tls_verify
        self._validate_remote_server()
        self.context = package.prepare_context(options)

    def _validate_remote_server(self):
        """
        Validates that the URL supplied points to a Chassis remote build server and that the
        version is new enough to support the new builds.

        This method is compatible with running unit tests and will skip this check if a magic
        value is used for the URL.

        Raises:
            If the request fails or the version is not new enough.
        """
        if self.url == "http://chassis-test-mode:9999":
            # Don't try to reach out to a real server during tests.
            return

        version_url = urllib.parse.urljoin(self.url, "/version")
        headers = {}
        if self.credentials:
            headers["Authorization"] = self.credentials
        res = requests.get(version_url, headers=headers, verify=self.tls_verify)
        parsed_version = version.parse(res.text)
        if parsed_version < version.Version('1.5.0'):
            warnings.warn("Chassis service version should be >=1.5.0 for compatibility with this SDK version, things may not work as expected. Please update the service.")

    def build_image(self, name: str, tag="latest", timeout: int = 3600, webhook: str = None, clean_context: bool = True, block_until_complete: bool = True) -> BuildResponse:
        """
        Starts a remote build of the container. When finished, the built image will be pushed
        to the registry that the remote builder is configured for (see the Chassis remote build
        server Helm chart for configuration options) with the name and tag supplied as arguments.

        By default, the build will be submitted with a timeout of one hour. You can change this
        value if desired. If the build takes longer than the timeout value, it will be canceled.

        An optional webhook can be supplied as well. A webhook is a URL that will be called by
        the remote build server with the result of the build (see `chassis.builder.BuildResponse`).

        Finally, at the end of this function, the Docker context that was created when the
        `RemoteBuilder` was initialized will be deleted by default. To prevent this, pass
        `clean_context=False` to this function.

        Args:
            name (str): Name of container image repository
            tag (str): Tag of container image
            timeout (int): Timeout value passed to build config object
            webhook (str): A URL that will be called when the remote build finishes
            clean_context (bool): If False does not remove build context folder
            block_until_complete (bool): If True, will block until the job is complete. To get an immediate response and poll for build completion yourself, set to False.

        Returns:
            BuildResponse: `BuildResponse` object with details from the build job

        Raises:
            ValueError: If webhook is not valid URL

        Examples:
        ```python
        from chassisml import ChassisModel
        from chassis.builder import RemoteBuilder, BuildOptions

        model = ChassisModel(process_fn=predict)
        options = BuildOptions(arch="arm64")
        builder = RemoteBuilder("http://localhost:8080", model, options)
        response = builder.build_image(name="chassis-model", tag="1.0.1")
        print(response)
        ```
        """
        if webhook is not None and not validators.url(webhook):
            raise ValueError("Provided webhook is not a valid URL")

        tmpdir = None
        build_context = None
        try:
            # Zip up the build context.
            tmpdir = tempfile.mkdtemp()
            package_basename = os.path.join(tmpdir, "package")
            package_filename = shutil.make_archive(package_basename, "zip", self.context.base_dir)

            # Construct the build arguments.
            build_config = {
                "image_tag": sanitize_image_name(name, tag),
                "platform": ",".join(self.context.platforms),
                "webhook": webhook,
                "timeout": timeout,
            }

            # Construct our request headers.
            headers = {
                "User-Agent": "ChassisClient/1.5"
            }
            if self.credentials is not None:
                headers["Authorization"] = self.credentials

            # Compile the files we're going to upload.
            build_context = open(package_filename, "rb")
            files = [
                ("build_config", json.dumps(build_config)),
                ("build_context", build_context),
            ]

            # Submit the build request.
            url = urllib.parse.urljoin(self.url, "/build")
            response = requests.post(url, headers=headers, files=files, verify=self.tls_verify)
            response.raise_for_status()

            obj = response.json()
            build_response = BuildResponse(**obj)
            print(f"Job has been submitted with id {build_response.remote_build_id}")

            if block_until_complete:
                build_response = self.block_until_complete()

            return build_response
        finally:
            # Clean up
            if clean_context:
                print("Cleaning local context")
                self.context.cleanup()
            if build_context is not None and not build_context.closed:
                build_context.close()
            if tmpdir is not None and os.path.exists(tmpdir):
                shutil.rmtree(tmpdir)

    def get_build_status(self, remote_build_id: str) -> BuildResponse:
        """
        Checks the status of a remote build.

        Args:
            remote_build_id (str): Remote build identifier generated from `RemoteBuilder.build_image` method

        Returns:
            BuildResponse: `BuildResponse` object with details from the build job

        Examples:
        ```python
        from chassisml import ChassisModel
        from chassis.builder import RemoteBuilder, BuildOptions

        model = ChassisModel(process_fn=predict)
        options = BuildOptions(arch="arm64")
        builder = RemoteBuilder("http://localhost:8080", model, options)
        response = builder.build_image(name="chassis-model", tag="1.0.1", block_until_complete=False)
        build_id = response.remote_build_id
        print(builder.get_build_status(build_id))
        ```
        """
        route = urllib.parse.urljoin(self.url, f"/jobs/{remote_build_id}")
        headers = {}
        if self.credentials:
            headers["Authorization"] = self.credentials
        res = requests.get(route, headers=headers, verify=self.tls_verify)
        data = res.json()
        return BuildResponse(**data)

    def get_build_logs(self, remote_build_id: str) -> str:
        """
        Checks the status of a chassis job
        Args:
            job_id (str): Chassis job identifier generated from `ChassisModel.publish` method

        Returns:
            Dict: JSON Chassis job status
        Examples:
        ```python
        # Create Chassisml model
        chassis_model = chassis_client.create_model(process_fn=process)
        # Define Dockerhub credentials
        dockerhub_user = "user"
        dockerhub_pass = "password"
        # Publish model to Docker registry
        response = chassis_model.publish(
            model_name="Chassisml Regression Model",
            model_version="0.0.1",
            registry_user=dockerhub_user,
            registry_pass=dockerhub_pass,
        )
        job_id = response.get('job_id')
        job_status = chassis_client.get_job_logs(job_id)
        ```
        """
        route = urllib.parse.urljoin(self.url, f"/jobs/{remote_build_id}/logs")
        headers = {}
        if self.credentials:
            headers["Authorization"] = self.credentials
        res = requests.get(route, headers=headers, verify=self.tls_verify)
        res.raise_for_status()
        return res.text

    def block_until_complete(self, remote_build_id: str, timeout=None, poll_interval=5) -> BuildResponse:
        """
        Blocks until Chassis job is complete or timeout is reached. Polls Chassis job API until a result is marked finished.

        Args:
            job_id (str): Chassis job identifier generated from `ChassisModel.publish` method
            timeout (int): Timeout threshold in seconds
            poll_interval (int): Amount of time to wait in between API polls to check status of job

        Returns:
            Dict: final job status returned by `ChassisClient.block_until_complete` method

        Examples:
        ```python
        # Create Chassisml model
        chassis_model = chassis_client.create_model(process_fn=process)

        # Define Dockerhub credentials
        dockerhub_user = "user"
        dockerhub_pass = "password"

        # Publish model to Docker registry
        response = chassis_model.publish(
            model_name="Chassisml Regression Model",
            model_version="0.0.1",
            registry_user=dockerhub_user,
            registry_pass=dockerhub_pass,
        )

        job_id = response.get('job_id')
        final_status = chassis_client.block_until_complete(job_id)
        ```
        """
        endby = time.time() + timeout if (timeout is not None) else None
        while True:
            status = self.get_build_status(remote_build_id)
            if status.completed:
                return status
            if (endby is not None) and (time.time() > endby - poll_interval):
                print('Timed out before completion.')
                return BuildResponse(image_tag=None, logs=None, success=False, completed=False, error_message="Timed out before completion.", remote_build_id=remote_build_id)
            time.sleep(poll_interval)
