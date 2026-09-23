"""
apps/core/models/ is a package (split from one models.py on 2026-09-23) whose
__init__.py re-exports every class, because `from apps.core.models import X`
is the only import style this codebase uses. These pin the two ways that
contract can quietly break when a model is added later.
"""

import inspect

from django.apps import apps
from django.db import models as dj_models

import apps.core.models as core_models


def test_every_model_defined_in_the_package_is_re_exported():
    defined = {
        name
        for module_name in ("sync", "hrs", "achhad", "vapi", "review", "auth", "reports", "ledgers", "consumption")
        for name, obj in vars(getattr(core_models, module_name)).items()
        if inspect.isclass(obj) and issubclass(obj, dj_models.Model) and obj.__module__.endswith(module_name)
    }
    missing = sorted(defined - set(core_models.__all__))
    assert not missing, f"Add these to apps/core/models/__init__.py's imports and __all__: {missing}"
    assert all(hasattr(core_models, name) for name in core_models.__all__)


def test_every_registered_core_model_is_importable_from_the_package():
    """Django's registry and the package agree - PTAuditLog is the one core
    model that lives outside the package (apps/core/audit_log.py) by design."""
    registered = {m.__name__ for m in apps.get_app_config("core").get_models()} - {"PTAuditLog"}
    assert registered == set(core_models.__all__)
