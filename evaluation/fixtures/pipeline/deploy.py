"""Deployment helper."""

import os


def target_environment():
    return os.environ.get("DEPLOY_ENV", "staging")


def is_production():
    return target_environment() == "production"
