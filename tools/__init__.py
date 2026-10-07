"""Harness-agnostic tooling for delegating adversarial review to the Antigravity CLI.

Module naming convention:

* ``antigravity_*`` modules are the delegation engine (``antigravity_bridge``,
  ``antigravity_mcp_server``, ``antigravity_live``) plus the widget server
  (``antigravity_viewer``).
* ``wisp_*`` modules are desktop-widget features (``wisp_chat`` for the chat
  store, ``wisp_capture`` for screen capture).
* ``skill_loader`` loads the review playbooks from the ``Skills/`` registry.
"""
