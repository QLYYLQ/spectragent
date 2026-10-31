"""Browser input layer — human-like mouse and keyboard middle layers.

``HumanMouse`` is the middle layer through which all cursor movement flows and
``HumanKeyboard`` the one through which all text entry flows. Both take a
conscribe-registered strategy — trajectory strategies (``instant`` /
``windmouse`` / ``bezier`` / ``learned``) chosen by the ``mouse_trajectory``
capability config, typing rhythms (``instant`` / ``human``) chosen by
``typing_rhythm`` — and both play it against the one deadline scheduler in
``_schedule.py``. In each pair the strategy *is* the humanization switch;
there is no boolean beside it to disagree with it.

This file imports nothing: the browser's ``declarations.py`` imports the two
registrar modules (``trajectory.py``, ``rhythm.py``) without loading the
controllers or the strategies. A strategy registers when its module is
imported, which discovery and ``capability.py`` do.
"""
