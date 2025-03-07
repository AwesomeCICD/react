import requests
import os
import argparse
import logging
import json
import gspread
import random
import time
import shutil
from logging import config
from urllib.parse import quote
from git.exc import GitCommandError
from io import BytesIO
from git import Repo, Git
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import sleep
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor

LOGGER = logging.getLogger(__name__)

def main():
    # Parse the arguments passed in
    args = get_args()

    # Configure logging
    load_logging_config(args["debug"], args["log_path"])

    LOGGER.debug("Parsed arguments successfully!")
    run(parsed_args=args)

def load_logging_config(debug, file_path):
    """
    Loads and configures a logging config
    :param debug: True or False if debugging for console should be turned on
    :param file_path: File path to storage the log file
    :return: None
    """
    logging_config = {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "debugFormater": {
                "format": "%(asctime)s.%(msecs)03d %(levelname)s [%(threadName)s]: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S"
            },
            "simpleFormater": {
                "format": "%(message)s"
            }
        },
        "handlers": {
            "file": {
                "class": "logging.FileHandler",
                "formatter": "debugFormater",
                "level": "DEBUG",
                "filename": "replay.log"
            },
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "simpleFormater",
                "level": "INFO",
                "stream": "ext://sys.stdout"
            }
        },
        "loggers": {
            "": {
                "level": "DEBUG",
                "handlers": ["file"]
            },
            "__main__": {
                "level": "DEBUG",
                "handlers": ["console"],
                "propagate": True
            }
        }
    }

    # If debugging, then switch the console format to be verbose
    if debug:
        logging_config["handlers"]["console"]["formatter"] = "debugFormater"

    # If a file path is passed in then handle the prefix and append the file name
    if file_path:
        log_path = Path(file_path)
        log_path = log_path.joinpath(logging_config["handlers"]["file"]["filename"])
        logging_config["handlers"]["file"]["filename"] = str(log_path)

    # Apply logging config
    logging.config.dictConfig(logging_config)
    LOGGER.debug("Logging Config: " + str(logging_config))
    LOGGER.debug("Logging is configured")

def get_args():
    """
    Processes and handles command line arguments
    :return: Dict of command line arguments
    """
    parser = argparse.ArgumentParser(description="Options for replaying commits")
    parser.add_argument("--github-repo-slug",
                        help="GitHub Repo slug in the form of OWNER/REPO",
                        type=str,
                        default=None,
                        required=False)
    parser.add_argument("--upstream-repo-url",
                        help="Upstream repo's URL",
                        type=str,
                        required=False)
    parser.add_argument("--working-repo-dir",
                        help="The directory of the forked repo",
                        type=str,
                        default="./",
                        required=False)
    parser.add_argument("--branch",
                       help="Name of the branch that will be used to run CI/CD pipelines",
                       type=str,
                       default=None)
    parser.add_argument("--github-token",
                        help="A valid GitHub token to be used for interacting with GitHub's API. Can also be set via env vars by using GITHUB_TOKEN",
                        type=str,
                        default=os.environ.get('GITHUB_TOKEN'),
                        required=False)
    parser.add_argument("--debug",
                        help="Enable Debug output",
                        action="store_true",
                        default=False)
    parser.add_argument("--log-path",
                        help="Path to where the log file will be generated",
                        type=str,
                        default=None)
    parser.add_argument("--config-path",
                        help="Path to where the config files are stored on main",
                        type=str,
                        default=None)
    parser.add_argument("--commit-delay",
                        help="Delay in between replay commits",
                        type=int,
                        default=10)
    parser.add_argument("--replay-days",
                        help="The amount of days worth of commits from the upstream repo to replay",
                        type=int,
                        default=1)

    args = parser.parse_args()

    return vars(args)

def run(parsed_args):
    """
    Main function that takes in arguments and processes them
    :param parsed_args: Dict of command line arguments
    """
    # Replay Commits In a Replay Branch
    branches, repo = replay_commits(parsed_args)
    LOGGER.info(f"Triggered commits on branch: {branches}")

def replay_commits(args):

    repo = Repo(args['working_repo_dir'])

    try:
        repo.create_remote('upstream', url=args['upstream_repo_url'])
    except Exception as e:
        print(f"Upstream remote already exists: {e}")
    repo.remotes.upstream.fetch("main")
    commits = []
    for commit in repo.iter_commits("upstream/main"):
        if commit.committed_datetime.date() < (datetime.now(timezone.utc) - timedelta(days=args['replay_days'])).date():
            break
        commits.append(commit)
    commits.reverse()
    print(f"The following commits will be processed:\n")
    print(*commits, sep="\n")

    branches = push_commits_one_by_one(args, repo, commits)
    
    return branches, repo

def push_commits_one_by_one(args, repo, commits):
    branches = []

    for commit in commits:
      if args['branch']:
        branch = f"{args['branch']}-{commit.hexsha}"
      else:
        current_date = datetime.now().date().isoformat()
        branch = f"replay-{current_date}-{commit.hexsha}"

      if branch not in repo.heads:
          repo.git.checkout('-B', branch, 'main')

      git_cmd = Git(args['working_repo_dir'])

      # configure local repo settings
      git_cmd.config('user.email', '')
      git_cmd.config('user.name', 'replay-bot')

      config_path = args['config_path']
      main_tree = repo.heads.main.commit.tree

      folders_from_main = {}
      for folder in [config_path]:
          for item in main_tree.traverse():
              if item.path.startswith(folder):
                  folders_from_main[item.path] = BytesIO(item.data_stream.read()).getvalue()

      repo.head.reset(commit=commit, index=True, working_tree=True)

      for path, data in folders_from_main.items():
          if os.path.exists(path):
              os.remove(path)
          export_blob(data, path)

      for folder in [".circleci"]:
          source_dir = os.path.join(args['working_repo_dir'], config_path, folder)
          destination_dir = os.path.join(args['working_repo_dir'], folder)

          if os.path.isdir(source_dir):
              if os.path.isdir(destination_dir):
                  shutil.rmtree(destination_dir)
      
              shutil.copytree(source_dir, destination_dir)

      repo.git.add('.')
      repo.index.commit(f"Committing {commit.hexsha}")

      url = repo.remotes.origin.url
      token = os.getenv('GITHUB_TOKEN')
      github_repo =  args['github_repo_slug']
      repo.remotes.origin.set_url(f'https://{quote(token)}:x-oauth-basic@github.com/{github_repo}')

      print(f"Pushing commit {commit.hexsha} to branch {branch}")
      repo.git.push("--force", "origin", branch)
      repo.remotes.origin.set_url(url)
      time.sleep(args['commit_delay'])

      branches.append(branch)

    return branches

def export_blob(data, dst_path):
    directory = os.path.dirname(dst_path)
    
    if os.path.exists(dst_path):
        if os.path.isfile(dst_path):
            # Remove file if it has the same name
            os.remove(dst_path)
        else:
            print(f"A directory with the name {dst_path} already exists.")
    else:
        try:
            os.makedirs(directory, exist_ok=True)
        except FileExistsError as e:
            # Remove directory if it has the same name
            os.remove(directory)
            os.makedirs(directory, exist_ok=True)
            
    if not os.path.isdir(dst_path):
        with open(dst_path, 'wb') as file:
            file.write(data)

if __name__ == "__main__":
    main()
