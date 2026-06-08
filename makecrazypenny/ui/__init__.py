"""Layer-2 UI surfaces for MakeCrazyPenny.

Currently a Streamlit dashboard (:mod:`makecrazypenny.ui.dashboard`) launched via
:func:`makecrazypenny.ui.launch.launch` (console script ``makecrazypenny-dashboard``).

The UI is a thin presentation layer: it calls the existing Layer-1 server *logic*
functions (which resolve data through the Layer-0 ``ProviderRegistry``) and renders
their plain-dict results. It adds no business logic of its own. Informational only;
not investment advice.
"""
