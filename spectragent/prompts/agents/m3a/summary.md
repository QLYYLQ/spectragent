{prefix}
The (overall) user goal/request is: {goal}
Now I want you to summerize the latest step.
You will be given the screenshot before you performed the action (which has a text label "before" on the bottom right), the action you chose (together with the reason) and the screenshot after the action was performed (which has a text label "after" on the bottom right).
Also here is the list of detailed information for some UI elements in the before screenshot:
{before_elements}
Here is the list for the after screenshot:
{after_elements}
This is the action you picked: {action}
Based on the reason: {reason}

By comparing the two screenshots (plus the UI element lists) and the action performed, give a brief summary of this step. This summary will be added to action history and used in future action selection, so try to include essential information you think that will be most useful for future action selections like what you intended to do, why, if it worked as expected, if not what might be the reason (be critical, the action/reason might be wrong), what should/should not be done next and so on. Some more rules/tips you should follow:
- Keep it short (better less than 50 words) and in a single line
- Some actions (like `answer`, `wait`) don't involve screen change, you can just assume they work as expected.
- Given this summary will be added into action history, it can be used as memory to include information that needs to be remembered, or shared between different apps.

Summary of this step: 