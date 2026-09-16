"""Hermes native plugin entry point. Importing this module performs no network work."""
from .jev_skills.hermes_adapter import register

__all__ = ['register']
