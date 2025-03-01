import json
import os
import shutil
import tempfile
import zipfile
import yaml


from . import BaseAction
from .... import ErsiliaBase
from ....utils.download import GitHubDownloader, S3Downloader
from ...bundle.repo import PackFile, DockerfileFile
from ....utils.exceptions_utils.throw_ersilia_exception import throw_ersilia_exception
from ....utils.exceptions_utils.fetch_exceptions import (
    FolderNotFoundError,
    S3DownloaderError,
)
from .template_resolver import TemplateResolver

from ....default import S3_BUCKET_URL_ZIP, PREDEFINED_EXAMPLE_FILES
from ....utils.paths import get_metadata_from_base_dir
from ....utils.logging import make_temp_dir

MODEL_DIR = "model"
ROOT = os.path.basename(os.path.abspath(__file__))


class PackCreator(ErsiliaBase):
    def __init__(self, model_id, config_json):
        ErsiliaBase.__init__(self, config_json=config_json, credentials_json=None)
        self.model_id = model_id
        self.dest_dir = self._model_path(self.model_id)

    def run(self):
        self.logger.debug("Copying pack creator")
        os.copy(
            os.path.join(ROOT, "..", "inner_template", "pack.py"),
            os.path.join(self.dest_dir, "pack.py"),
        )


class ServiceCreator(ErsiliaBase):
    def __init__(self, model_id, config_json):
        ErsiliaBase.__init__(self, config_json=config_json, credentials_json=None)
        self.model_id = model_id
        self.dest_dir = self._model_path(self.model_id)

    def run(self):
        self.logger.debug("Creating service.py file")
        src_dir = os.path.join(self.dest_dir, "src")
        if not os.path.exists(src_dir):
            os.mkdir(src_dir)
        
        with open(os.path.join(self.dest_dir, "src", "service.py"), "r") as f:
            content = f.read()
        
        
        try:
            metadata = get_metadata_from_base_dir(self.dest_dir)
            output_type = metadata["Output Type"]
        except (FileNotFoundError, KeyError):
            output_type = None
        outcome_line_index = -1
        splited = content.splitlines()
        for i, line in enumerate(splited):
            if "outcome" in line:
                outcome_line_index = i
                break
        output = None
        if output_type == "String":
            splited[outcome_line_index] = splited[outcome_line_index].replace(
                "Float", "String"
            )
        output = "\n".join(splited)
        file_path = os.path.join(self.dest_dir, "src", "service.py")
        with open(file_path, "w") as f:
            f.write(output)


class DockerfileCreator(ErsiliaBase):
    def __init__(self, model_id, config_json, commands):
        ErsiliaBase.__init__(self, config_json=config_json, credentials_json=None)
        self.model_id = model_id
        self.dest_dir = self._model_path(self.model_id)

    def run(self):
        self.logger.debug(
            "Creating Dockerfile for internal usage from a config.yml file"
        )
        file_path = os.path.join(self.dest_dir, "Dockerfile")
        with open(file_path, "r") as f:
            docker_file = f.read()

        updated = self._append_commands_to_dockerfile(self.commands, docker_file)
        with open(file_path, "w") as f:
            f.write(updated)

    def _append_commands_to_dockerfile(self, commands, dockerfile_content):
        run_section_end_line = -1
        sanitized = []
        for i, line in enumerate(dockerfile_content.splitlines()):
            line = line.strip()
            if line.startswith("MAINTAINER") and line.endswith("ersilia"):
                run_section_end_line = i
            if line != "":
                sanitized.append(line)
        if sanitized[run_section_end_line + 2].startswith("RUN"):
            raise BaseException(
                "There are more instalation than the standard 'RUN pip install rdkit'"
            )

        if run_section_end_line != -1:
            index = run_section_end_line + 2
            for i, command in enumerate(commands):
                sanitized.insert(index + i, f"RUN {command}")
            return "\n".join(sanitized)
        else:
            BaseException(
                "Error: Unable to locate the end of the RUN section in the Dockerfile."
            )


class TemplatePreparer(BaseAction):
    def __init__(self, model_id, config_json):
        BaseAction.__init__(
            self, model_id=model_id, config_json=config_json, credentials_json=None
        )
        self.dest_dir = self._model_path(self.model_id)
        self.config_yaml_path = "config.yml"

    def _create_pack(self):
        pack_file = os.path.join(self.dest_dir, "pack.py")
        if os.path.exists(pack_file):
            self.logger.debug("The pack.py file already exists")
            return
        PackCreator(model_id=self.model_id, config_json=self.config_json).run()

    def _create_service(self):
        src_folder = os.path.join(self.dest_dir, "src")
        if os.path.exists(src_folder):
            self.logger.debug("The src folder already exists")
            return
        service_file = os.path.join(src_folder, "service.py")
        if os.path.exists(service_file):
            self.logger.debug("The service.py file already exists")
            return
        if not os.path.exists(self.dest_dir, "model", "framework", "run.sh"):
            self.logger.debug(
                "The run.sh file, which is assumed by service.py, does not exist"
            )
            return
        ServiceCreator(model_id=self.model_id, config_json=self.config_json).run()

    def _create_dockerfile(self):
        dockerfile_file = os.path.join(self.dest_dir, "Dockerfile")
        if os.path.exists(dockerfile_file):
            self.logger.debug("The Dockerfile file already exists")
            return
        DockerfileCreator(model_id=self.model_id, config_json=self.config_json).run()

    def prepare(self):
        self._create_pack()
        self._create_dockerfile()
        self._create_service()

class ModelRepositoryGetter(BaseAction):
    def __init__(
        self, model_id, config_json, force_from_github, force_from_s3, repo_path
    ):
        BaseAction.__init__(
            self, model_id=model_id, config_json=config_json, credentials_json=None
        )
        self.token = self.cfg.HUB.TOKEN
        self.github_down = GitHubDownloader(self.token)
        self.s3_down = S3Downloader()
        self.org = self.cfg.HUB.ORG
        self.force_from_github = force_from_github
        self.force_from_s3 = force_from_s3
        self.repo_path = repo_path

    @staticmethod
    def _copy_from_local(src, dst):
        shutil.copytree(src, dst)

    def _copy_from_github(self, dst):
        self.github_down.clone(org=self.org, repo=self.model_id, destination=dst)

    def _copy_zip_from_s3(self, dst):
        self.logger.debug("Downloading model from S3 in zipped format")
        tmp_file = os.path.join(make_temp_dir("ersilia-"), "model.zip")
        self.s3_down.download_from_s3(
            bucket_url=S3_BUCKET_URL_ZIP,
            file_name=self.model_id + ".zip",
            destination=tmp_file,
        )
        self.logger.debug("Extracting model from {0}".format(tmp_file))
        dst = "/".join(dst.split("/")[:-1])
        self.logger.debug("...to {0}".format(dst))
        with zipfile.ZipFile(tmp_file, "r") as zip_ref:
            zip_ref.extractall(dst)

    def _change_py_version_in_dockerfile_if_necessary(self):
        self.logger.debug("Changing python version if necessary")
        path = self._model_path(model_id=self.model_id)
        df = DockerfileFile(path=path)
        version = df.get_bentoml_version()
        self.logger.debug(version)
        dockerfile_path = os.path.join(path, "Dockerfile")
        with open(dockerfile_path, "r") as f:
            R = f.readlines()
        S = []
        for r in R:
            if r.startswith("FROM "):
                r = r.split("-")
                if r[-1].startswith("py"):
                    p = version["python"]
                    r = "-".join(r[:-1] + [p])
                else:
                    r = "-".join(r)
            S += [r]
        with open(dockerfile_path, "w") as f:
            for s in S:
                f.write(s + os.linesep)

    @staticmethod
    def _is_root_user():
        return os.geteuid() == 0

    def _remove_sudo_if_root(self):
        path = self._model_path(model_id=self.model_id)
        dockerfile_path = os.path.join(path, "Dockerfile")
        if self._is_root_user():
            self.logger.debug("User is root! Removing sudo commands")
            with open(dockerfile_path, "r") as f:
                content = f.read()
                content = content.replace("RUN sudo ", "RUN ")
                content = content.replace(" sudo ", " ")
                content = content.replace(";sudo ", "; ")
                content = content.replace("&sudo ", "& ")
                content = content.replace("|sudo ", "| ")
            with open(dockerfile_path, "w") as f:
                f.write(content)
        else:
            self.logger.debug("User is not root")

    def _prepare_inner_template(self):
        self.logger.debug("Preparing inner template if necessary")
        TemplatePreparer(model_id=self.model_id, config_json=self.config_json).prepare()

    def _copy_example_file_if_available(self):
        self.logger.debug("Copying example file if available")
        for pf in PREDEFINED_EXAMPLE_FILES:
            file_name = os.path.join(self._model_path(self.model_id), pf)
            dest_file = os.path.join(self._model_path(self.model_id), "input.csv")
            if os.path.exists(file_name):
                self.logger.debug("Example file exists")
                shutil.copy(file_name, dest_file)
                return
            else:
                self.logger.debug("Example file {0} does not exist".format(file_name))

    @throw_ersilia_exception
    def get(self):
        """Copy model repository from local or download from S3 or GitHub"""
        folder = self._model_path(self.model_id)
        tr = TemplateResolver(
            model_id=self.model_id, repo_path=folder, config_json=self.config_json
        )
        if self.repo_path is not None:
            self._copy_from_local(self.repo_path, folder)
        else:
            if self.force_from_github:
                self._copy_from_github(folder)
            else:
                try:
                    self.logger.debug("Trying to download from S3")
                    self._copy_zip_from_s3(folder)
                except:
                    self.logger.debug(
                        "Could not download in zip format in S3. Downloading from GitHub repository."
                    )
                    if self.force_from_s3:
                        raise S3DownloaderError(model_id=self.model_id)
                    else:
                        self._copy_from_github(folder)

        if tr.is_bentoml():
            self._prepare_inner_template()
            self._change_py_version_in_dockerfile_if_necessary()

        self._remove_sudo_if_root()
        self._copy_example_file_if_available()


#  TODO: work outside GIT LFS
class ModelParametersGetter(BaseAction):
    def __init__(self, model_id, config_json):
        BaseAction.__init__(
            self, model_id=model_id, config_json=config_json, credentials_json=None
        )

    @staticmethod
    def _requires_parameters(model_path):
        pf = PackFile(model_path)
        return pf.needs_model()

    def _get_destination(self):
        model_path = self._model_path(self.model_id)
        return os.path.join(model_path, MODEL_DIR)

    @throw_ersilia_exception
    def get(self):
        """Create a ./model folder in the model repository"""
        model_path = self._model_path(self.model_id)
        folder = self._get_destination()
        tr = TemplateResolver(
            model_id=self.model_id, repo_path=model_path, config_json=self.config_json
        )
        if tr.is_fastapi():
            return None
        if not os.path.exists(folder):
            os.mkdir(folder)
        if not self._requires_parameters(model_path):
            return None
        if not os.path.exists(folder):
            raise FolderNotFoundError(folder)


class ModelGetter(BaseAction):
    def __init__(
        self, model_id, repo_path, config_json, force_from_gihtub, force_from_s3
    ):
        BaseAction.__init__(
            self, model_id=model_id, config_json=config_json, credentials_json=None
        )
        self.model_id = model_id
        self.repo_path = repo_path
        self.mrg = ModelRepositoryGetter(
            model_id=model_id,
            config_json=config_json,
            force_from_github=force_from_gihtub,
            force_from_s3=force_from_s3,
            repo_path=repo_path,
        )
        self.mpg = ModelParametersGetter(model_id=model_id, config_json=config_json)

    def _get_repository(self):
        self.mrg.get()

    def _get_model_parameters(self):
        self.mpg.get()

    @throw_ersilia_exception
    def get(self):
        self.logger.debug("Getting repository")
        self._get_repository()
        if self.repo_path is None:
            self.logger.debug("Getting model parameters")
            self._get_model_parameters()
        self.logger.debug("Done getting model")
        if not os.path.exists(self._model_path(self.model_id)):
            raise FolderNotFoundError(os.path.exists(self._model_path(self.model_id)))
