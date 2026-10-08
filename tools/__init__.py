"""Harness-agnostic tooling for delegating adversarial review to the Antigravity CLI.

Module naming convention:

* ``antigravity_*`` modules are the delegation engine (``antigravity_bridge``
  with ``antigravity_containment`` and ``antigravity_aggregate``,
  ``antigravity_mcp_server``, ``antigravity_live``) plus the widget server
  (``antigravity_viewer``).
* ``viewer_*`` modules support the widget server (``viewer_shell`` for the
  window shells, ``viewer_platform`` for OS process and account helpers).
* ``wisp_*`` modules are desktop-widget features (``wisp_chat`` for the chat
  store, ``wisp_capture`` for screen capture).
* ``skill_loader`` loads the review playbooks from the ``Skills/`` registry.
"""
