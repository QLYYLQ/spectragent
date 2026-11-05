"""VenusBench-Mobile's pinned code, and the tasks of it the first version runs (``docs/architecture_design.md`` §11.2 "VenusBench-Mobile 的接入方案").

VenusBench-Mobile is a fork of AndroidWorld (Apache-2.0, its ``NOTICE`` says
so) on the ``VenusBench-Mobile`` branch of ``inclusionAI/UI-Venus``, pinned at
:data:`VENUSBENCH`. Its tasks are AndroidWorld's ``TaskEval`` classes, all in
its ``venus`` family: 189 of them at the pinned commit, its README's 149 and
40 language variants (``...CHS``, ``...ENGVariation``). Its own runner makes
the suite with seed 30 and one instance of each task, as the task lists do
(:data:`SUITE_SEED`).

The first version runs the 45 single-round tasks scored on the device's state
or by a rule on the answer: every task the pinned commit lists that is not in
:data:`UNSUPPORTED`. The other 144 are there, each with why: the loader leaves them out with that
reason in its ``skipped``, and the setup component refuses them in
``prepare()`` with the same words. The tables were read off the pinned code,
each class's ``max_round``, the judge its ``is_successful`` reaches, its
``check_status`` and what its score reads, on 2026-11-04.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from spectragent.benchmarks.androidworld.service import PinnedCode

#: VenusBench-Mobile's repository and the commit pinned: the head of its branch on 2026-05-11.
VENUSBENCH = PinnedCode(
    benchmark="venusbench",
    name="VenusBench",
    repository="https://github.com/inclusionAI/UI-Venus.git",
    commit="5b2c618ef146ea38890ea35dca8b07ec2d0284dd",
)
#: The family that holds every VenusBench task.
FAMILY = "venus"
#: The suite seed of VenusBench's own runner (``run_venusbenchnavi.py``'s default).
SUITE_SEED = 30

_MULTI_ROUND_WHY = (
    "a multi-round task: its runner gives the agent the next round's goal once it declares a "
    "round done, and a run's task text is set once, after setup, and judged once, after the "
    "agent's run (docs/architecture_design.md §4.2, §4.10)"
)
_UNFINISHED_WHY = (
    "a multi-round task whose class sets no max_round, so its second round, which its score "
    "checks, is never reached: it scores 1 when nothing is done and 0 when the notes its goal"
    " asks for are made"
)
_LLM_JUDGE_WHY = (
    "VenusBench's LLM judge scores it, and that judge cannot use a model configured here: its"
    " LLMServer hardcodes the key and the model name, sends arguments only vLLM accepts, and "
    "reads only a URL from the checkout's settings"
)
_BEFORE_EACH_STEP_WHY = (
    "it changes the device before each of the agent's steps (its check_status: a call, a "
    "rotation, an app killed or opened), and the setup component has no hook before a step"
)
_UIAUTOMATOR2_WHY = (
    "its score reads the screen with uiautomator2, which reaches the device through adb's "
    "default server on port 5037 instead of the run's own"
)

#: Changes the goal between rounds.
_MULTI_ROUND = (
    "MultiRoundExpense1Add2Add3Delete",
    "MultiRoundMarkorAdd4NotesDeleteNote",
    "MultiRoundMarkorAppendCopyRename",
    "MultiRoundMarkorAppendCopyRenameCHS",
    "MultiRoundMarkorAppendCopyRenameVariation",
    "MultiRoundMarkorCreateNoteAndSms",
    "MultiRoundMarkorCreateNoteHeading",
    "MultiRoundMarkorCreateTwoNote",
    "MultiRoundRecipe1AddImageRecipes2AddMarkor3DeleteImage",
    "MultiRoundRetroSavePlaylist",
    "MultiRoundRetroSavePlaylistCHS",
    "MultiRoundRetroSavePlaylistVariation",
    "MultiRoundVlcCreateTwoPlaylistsReverse",
)
#: Scored by VenusBench's LLM judge.
_LLM_JUDGE = (
    "BrowsecompFindAppTaskEvalUI1",
    "BrowsecompFindAppTaskEvalUI1CHS",
    "BrowsecompFindAppTaskEvalUI1Variation",
    "BrowsecompFindAppTaskEvalUI2",
    "BrowsecompFindAppTaskEvalUI3",
    "BrowsecompFindAppandAskInfo1",
    "BrowsecompFindAppandRelatedAppandAskInfo1",
    "BrowsecompFindImage",
    "BrowsecompFindVideo",
    "BrowsecompOpenAppTaskEvalUICompCreate1",
    "BrowsecompOpenAppTaskEvalUICompCreate2",
    "BrowsecompVlcFindVlcAPP1",
    "BrowsecompVlcFindVlcAPP1CHS",
    "BrowsecompVlcFindVlcAPP1Variation",
    "FuncAssistExpenseExplainAllFunctionality",
    "FuncAssistExpenseExplainOneFunctionality1",
    "FuncAssistExpenseExplainOneFunctionality1CHS",
    "FuncAssistExpenseExplainOneFunctionality1Variation",
    "FuncAssistExpenseLocateOneFunctionality1",
    "FuncAssistFitbookLocateOneFunctionality",
    "FuncAssistJoblinExplainOneFunctionality1",
    "FuncAssistLocateSystemInterfaceTaskEval2",
    "FuncAssistLocateTasksInterface1",
    "FuncAssistMarkorExplainOneFunctionality1",
    "FuncAssistMarkorExplainOneFunctionality2",
    "FuncAssistMarkorExplainOneFunctionality3",
    "FuncAssistMarkorJoplinTasks",
    "FuncAssistMarkorLocateOneFunctionality1",
    "FuncAssistMedilogExplainAllFunctionality",
    "FuncAssistOpenTracksExplainOneFunctionality",
    "FuncAssistRecipeExplainOneFunctionality1",
    "FuncAssistRecipeLocateOneFunctionality1",
    "FuncAssistRefusalMarkorExplainOneFunctionality",
    "FuncAssistTasksExplainOneFunctionality1",
    "FuncAssistVagueRecipeExplainOneFunctionality",
    "FuncAssistVlcExplainOneFunctionality1",
    "FuncAssistVlcLocateOneFunctionality1",
    "FuncAssistVlcLocateOneFunctionality1CHS",
    "FuncAssistVlcLocateOneFunctionality1Variation",
    "FuncAssistZipxtractExplainOneFunctionality",
    "GUIBrowsingAnswer1",
    "GUIBrowsingAnswer2",
    "GUIBrowsingFindGameFromGalleryTaskEvalUI",
    "GUIBrowsingFindImageInPaper1",
    "GUIBrowsingFindImageInPaper2",
    "GUIBrowsingFindPDF1",
    "GUIBrowsingFindPDF2",
    "GUIBrowsingFitbookCalories",
    "GUIBrowsingOnlineShopping1",
    "GUIBrowsingOnlineShopping2",
    "GUIBrowsingOrder1",
    "GUIBrowsingOrder2",
    "GUIBrowsingOrder2CHS",
    "GUIBrowsingOrder2Variation",
    "GUIBrowsingOrder3",
    "GUIBrowsingPDF1",
    "GUIBrowsingPDF2",
    "GUIBrowsingPaper1",
    "GUIBrowsingPaper1CHS",
    "GUIBrowsingPaper1Variation",
    "GUIBrowsingPaper2",
    "GUIBrowsingPaper3",
    "GUIBrowsingRefusalVlcWatchVideo",
    "GUIBrowsingVagueVlcWatchVideo",
    "GUIBrowsingWatchVideo2",
    "GUIBrowsingWatchVideo4",
    "GUIBrowsingWatchVideo5",
    "GUIBrowsingWatchVideo6",
    "GUIMChangePicture",
    "GUIMCircleObject1",
    "GUIMCircleObject2",
    "GUIMDrawA",
    "GUIMDrawACHS",
    "GUIMDrawAENGVariation",
    "GUIMDrawRectangleHard",
    "GUIMEraseObject1",
    "GUIMEraseObject1CHS",
    "GUIMEraseObject1ENGVariation",
    "GUIMEraseObject2",
    "GUIMJoplinDrawCircle",
    "GUIMJoplinDrawCircleAndRectangle",
    "RefusalAudioRecorderRecordAudioWithFileNameConflict1",
    "RefusalAudioRecorderRecordAudioWithFileNameConflict2",
    "RefusalCameraTakeVideoConflict1",
    "RefusalCameraTakeVideoConflict2",
    "RefusalExpenseDeleteMultipleConflictAll",
    "RefusalExpenseDeleteMultipleConflictMultiple",
    "RefusalExpenseDeleteMultipleConflictSingle",
    "RefusalMarkorDeleteMultipleNotesAmbigious1",
    "RefusalMarkorDeleteMultipleNotesAmbigious1CHS",
    "RefusalMarkorDeleteMultipleNotesAmbigious1Variation",
    "RefusalMarkorDeleteMultipleNotesAmbigious2",
    "RefusalMarkorDeleteMultipleNotesConflict1",
    "RefusalRecipeAddSingleRecipeConflict1",
    "RefusalRecipeDeleteMultipleRecipesWithConstraintAmbigious1",
    "RefusalRetroPlayingQueueConflict1",
    "RefusalSimpleCalendarAddOneEventDateAmbigious1",
    "RefusalSimpleCalendarAddOneEventInTwoWeeksConflict1",
    "RefusalSystemBluetoothTurnOffAlreadyOff",
    "RefusalSystemBluetoothTurnOnAlreadyOn",
    "RefusalTasksAddOneTaskAmbigious1",
    "RefusalTomotoSettingConflict1",
    "RefusalTomotoSettingConflict2",
    "RefusalZipxtractCreateFile",
    "VagueFindPhoneTaskEvalUI",
    "VagueGraphFunction",
    "VagueWatchLocalVideo",
)
#: Changes the device before each step.
_BEFORE_EACH_STEP = (
    "NoiseResistAudioRecorderRecordAudioWithFilenameCall",
    "NoiseResistContactsAddContactWithCall",
    "NoiseResistExpenseAddSingleADs",
    "NoiseResistExpenseAddSingleAPPCollapse",
    "NoiseResistExpenseAddSingleAPPNumb",
    "NoiseResistExpenseAddSingleWithCall",
    "NoiseResistExpenseAddSingleWithOrientation",
    "NoiseResistExpenseDeleteSingleWithOrientation",
    "NoiseResistExpenseDeleteSingleWithOrientationCHS",
    "NoiseResistExpenseDeleteSingleWithOrientationVariation",
    "NoiseResistFilesMoveFileAPPCollapse",
    "NoiseResistFilesMoveFileAPPCollapseCHS",
    "NoiseResistFilesMoveFileAPPCollapseVariation",
    "NoiseResistMarkorCreateNoteWithOrientation",
    "NoiseResistOsmAndMarkerAPPCollapse",
    "NoiseResistRetroPlayingQueueAPPCollapse",
    "NoiseResistSimpleCalendarAddOneEventTomorrowWithOrientation",
    "NoiseResistSimpleCalendarAddRepeatingEventCallandAPPCollapse",
    "NoiseResistVlcCreatePlaylistWithOrientation",
    "NoiseResistVlcCreateTwoPlaylistsWithCallandCollapse",
)
#: Scored through uiautomator2.
_UIAUTOMATOR2 = (
    "GSAClockStopWatchPausedatCertainTime",
    "GSATimingAudioRecorderPauseAudioRecordingTime",
    "GSATimingTomatoLongBreakTime",
)

#: The tasks of the pinned commit the first version does not run: task key → why.
UNSUPPORTED: Mapping[str, str] = MappingProxyType(
    {
        **dict.fromkeys(_MULTI_ROUND, _MULTI_ROUND_WHY),
        "MultiRoundMarkorCreateNoteReverse": _UNFINISHED_WHY,
        **dict.fromkeys(_LLM_JUDGE, _LLM_JUDGE_WHY),
        **dict.fromkeys(_BEFORE_EACH_STEP, _BEFORE_EACH_STEP_WHY),
        **dict.fromkeys(_UIAUTOMATOR2, _UIAUTOMATOR2_WHY),
    }
)

__all__ = ["FAMILY", "SUITE_SEED", "UNSUPPORTED", "VENUSBENCH"]
