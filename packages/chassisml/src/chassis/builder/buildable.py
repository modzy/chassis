from __future__ import annotations

import abc
import os
import posixpath
import shutil
import subprocess
import sys
from shutil import copy, copytree
from typing import List, Union

import cloudpickle
from jinja2 import Environment, PackageLoader, select_autoescape

from chassis.builder import BuildContext
from chassis.metadata import ModelMetadata
from chassis.runtime import PACKAGE_DATA_PATH, python_pickle_filename_for_key
from .options import BuildOptions, DefaultBuildOptions
from .errors import RequiredFieldMissing

env = Environment(
    loader=PackageLoader(package_name="chassis.builder"),
    autoescape=select_autoescape()
)

REQUIREMENTS_SUBSTITUTIONS = {
    "opencv-python=": "opencv-python-headless="
}


def _copy_libraries(context: BuildContext, server: str, ignore_patterns: List[str]):
    root = os.path.join(os.path.dirname(__file__), "..", "..")
    ignore = shutil.ignore_patterns(*ignore_patterns)
    copytree(os.path.join(root, "chassis", "runtime"), os.path.join(context.chassis_dir, "runtime"), ignore=ignore)
    copytree(os.path.join(root, "chassis", "metadata"), os.path.join(context.chassis_dir, "metadata"), ignore=ignore)
    copytree(os.path.join(root, "chassis", "typing"), os.path.join(context.chassis_dir, "typing"), ignore=ignore)
    copytree(os.path.join(root, "chassis", "server", server), os.path.join(context.chassis_dir, "server", server), ignore=ignore)


class Buildable(metaclass=abc.ABCMeta):
    packaged = False
    metadata = ModelMetadata.default()
    requirements: set[str] = set()
    apt_packages: set[str] = set()
    additional_files: set[str] = set()
    python_modules: dict = {}

    def merge_package(self, package: Buildable):
        '''
        TODO - internal?
        '''
        self.requirements = self.requirements.union(package.requirements)
        self.apt_packages = self.apt_packages.union(package.apt_packages)
        self.additional_files = self.additional_files.union(package.additional_files)
        self.python_modules.update(package.python_modules)

    def add_requirements(self, reqs: Union[str, list]):
        '''This function adds python dependencies to a `Buildable` model object
        
        Args:
            reqs: (Union[str, list]): Single python package (str) or list of python packages that are required dependencies to run the `ChassisModel.process_fn` attribute. These values are the same values that would follow `pip install` or that would be added to a Python dependencies txt file (e.g., `requirements.txt`) 
        '''
        if type(reqs) == str:
            self.requirements = self.requirements.union(reqs.splitlines())
        elif type(reqs) == list:
            self.requirements = self.requirements.union(reqs)

    def add_apt_packages(self, packages: Union[str, list]):
        '''This function adds OS-level dependencies to a `Buildable` model object
        
        Args:
            reqs: (Union[str, list]): Single OS-level package (str) or list of OS-level packages that are required dependencies to run the `ChassisModel.process_fn` attribute. These values are the same values that can be installed via `apt-get install` 
        '''
        if type(packages) == str:
            self.apt_packages = self.apt_packages.union(packages.splitlines())
        elif type(packages) == list:
            self.apt_packages = self.apt_packages.union(packages)

    def get_packaged_path(self, path: str):
        '''
        TODO - internal?
        '''
        return posixpath.join(PACKAGE_DATA_PATH, os.path.basename(path))

    def verify_prerequisites(self, options: BuildOptions):
        '''
        TODO - internal?
        '''
        if len(self.metadata.model_name) == 0:
            raise RequiredFieldMissing("The model must have a name set before it can be built. Please set `metadata.model_name`.")
        if len(self.metadata.model_version) == 0:
            raise RequiredFieldMissing("The model must have a version set before it can be built. Please set `metadata.model_version`.")
        if not self.metadata.has_inputs():
            raise RequiredFieldMissing("The model must have at least one input defined before it can be built. Please call `metadata.add_input()`.")
        if not self.metadata.has_outputs():
            raise RequiredFieldMissing("The model must have at least one output defined before it can be built. Please call `metadata.add_output()`.")
        if options.cuda_version is not None and options.python_version != "3.8":
            print(f"Warning: Building a container with CUDA currently only supports Python 3.8. Python 3.8 will be used instead of '{options.python_version}'.")

    def prepare_context(self, options: BuildOptions = DefaultBuildOptions) -> BuildContext:
        '''
        TODO - internal?
        '''
        self.verify_prerequisites(options)

        platforms = []
        if isinstance(options.arch, str):
            platforms = [f"linux/{options.arch}"]
        elif isinstance(options.arch, list):
            platforms = [f"linux/{a}" for a in options.arch]
        context = BuildContext(base_dir=options.base_dir, platforms=platforms)

        print("Using local context: " + context.base_dir)
        # Ensure the target directories exist.
        if not os.path.exists(context.chassis_dir):
            os.makedirs(context.chassis_dir, exist_ok=True)
        if not os.path.exists(context.data_dir):
            os.makedirs(context.data_dir, exist_ok=True)

        # Render and save Dockerfile to package location.
        print("Generating Dockerfile...", end="", flush=True)
        rendered_template = self.render_dockerfile(options)
        with open(os.path.join(context.base_dir, "Dockerfile"), "wb") as f:
            f.write(rendered_template.encode())

        # Save .dockerignore to package location.
        dockerignore_template = env.get_template(".dockerignore")
        dockerignore = dockerignore_template.render()
        with open(os.path.join(context.base_dir, ".dockerignore"), "wb") as f:
            f.write(dockerignore.encode())

        # Save the entrypoint file.
        entrypoint_template = env.get_template("entrypoint.py")
        with open(os.path.join(context.base_dir, "entrypoint.py"), "wb") as f:
            f.write(entrypoint_template.render().encode())
        print("Done!")

        # Copy any Chassis libraries we need.
        print("Copying libraries...", end="", flush=True)
        _copy_libraries(context, options.server, dockerignore.splitlines())
        print("Done!")
        print("Writing metadata...", end="", flush=True)
        self._write_metadata(context)
        print("Done!")
        print("Compiling pip requirements...", end="", flush=True)
        self._write_requirements(context, options)
        print("Done!")
        print("Copying files...", end="", flush=True)
        self._write_additional_files(context)
        self._write_python_modules(context)
        print("Done!")

        return context

    def render_dockerfile(self, options: BuildOptions) -> str:
        '''
        TODO - internal?
        '''
        dockerfile_template = env.get_template("Dockerfile")
        run_apt_get = ""
        if len(self.apt_packages) > 0:
            apt_package_list = " ".join(self.apt_packages)
            run_apt_get = f"RUN apt-get update && apt-get install -y {apt_package_list} && rm -rf /var/lib/apt/lists/*"

        return dockerfile_template.render(
            python_version=options.python_version,
            cuda_version=options.cuda_version,
            apt_packages=run_apt_get,
        )

    def _write_additional_files(self, context: BuildContext):
        for file in self.additional_files:
            copy(file, os.path.join(context.data_dir, os.path.basename(file)))

    def _write_requirements(self, context: BuildContext, options: BuildOptions):
        requirements_template = env.get_template("requirements.txt")
        # Convert to a set then back to a list to unique the entries in case there are duplicates.
        additional_requirements = list(set(self.requirements))
        # Append the server requirements.
        if options.server == "omi":
            from chassis.server.omi import REQUIREMENTS
            additional_requirements.extend(REQUIREMENTS)
        elif options.server == "kserve":
            from chassis.server.kserve import REQUIREMENTS
            additional_requirements.extend(REQUIREMENTS)
        # Sort the list so that our requirements.txt is stable for Docker caching.
        additional_requirements.sort()
        rendered_template = requirements_template.render(
            additional_requirements="\n".join(additional_requirements)
        )
        requirements_in = os.path.join(context.base_dir, "requirements.in")
        requirements_txt = os.path.join(context.base_dir, "requirements.txt")
        with open(requirements_in, "wb") as f:
            f.write(rendered_template.encode("utf-8"))
        # Use pip-tools to expand the list out to a frozen and pinned list.
        subprocess.run([sys.executable, "-m", "piptools", "compile", "-q", "-o", requirements_txt, requirements_in])
        # Now post-process the full requirements.txt with automatic replacements.
        with open(requirements_txt, "rb") as f:
            reqs = f.read().decode()
        for old, new in REQUIREMENTS_SUBSTITUTIONS.items():
            reqs = reqs.replace(old, new)
        with open(requirements_txt, "wb") as f:
            f.write(reqs.encode())

    def _write_python_modules(self, context: BuildContext):
        for key, m in self.python_modules.items():
            output_filename = python_pickle_filename_for_key(key)
            with open(os.path.join(context.data_dir, output_filename), "wb") as f:
                cloudpickle.dump({key: m}, f)

    def _write_metadata(self, context: BuildContext):
        data = self.metadata.serialize()
        with open(os.path.join(context.data_dir, "model_info"), "wb") as f:
            f.write(data)
