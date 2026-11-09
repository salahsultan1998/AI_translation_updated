"""Shared authentication switch for the web app and job worker."""
import os


def auth_enabled():
    """Only explicitly false values disable authentication; missing or invalid values enable it."""
    return os.environ.get('LOCATOOL_AUTH_ENABLED', 'true').strip().lower() not in {'0', 'false', 'no', 'off'}
