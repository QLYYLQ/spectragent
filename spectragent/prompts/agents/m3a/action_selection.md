{prefix}
The current user goal/request is: {goal}

Here is a history of what you have done so far:
{history}

The current screenshot and the same screenshot with bounding boxes and labels added are also given to you.
Here is a list of detailed information for some of the UI elements (notice that some elements in this list may not be visible in the current screen and so you can not interact with it, can try to scroll the screen to reveal it first), the numeric indexes are consistent with the ones in the labeled screenshot:
{ui_elements}
Here are some useful guidelines you need to follow:
General:
- Usually there will be multiple ways to complete a task, pick the easiest one. Also when something does not work as expected (due to various reasons), sometimes a simple retry can solve the problem, but if it doesn't (you can see that from the history), SWITCH to other solutions.
- Sometimes you may need to navigate the phone to gather information needed to complete the task, for example if user asks "what is my schedule tomorrow", then you may want to open the calendar app (using the `open_app` action), look up information there, answer user's question (using the `answer` action) and finish (using the `status` action with complete as goal_status).
- For requests that are questions (or chat messages), remember to use the `answer` action to reply to user explicitly before finish! Merely displaying the answer on the screen is NOT sufficient (unless the goal is something like "show me ...").
- If the desired state is already achieved (e.g., enabling Wi-Fi when it's already on), you can just complete the task.
Action Related:
- Use the `open_app` action whenever you want to open an app (nothing will happen if the app is not installed), do not use the app drawer to open an app unless all other ways have failed.
- Use the `input_text` action whenever you want to type something (including password) instead of clicking characters on the keyboard one by one. Sometimes there is some default text in the text field you want to type in, remember to delete them before typing.
- For `click`, `long_press` and `input_text`, the index parameter you pick must be VISIBLE in the screenshot and also in the UI element list given to you (some elements in the list may NOT be visible on the screen so you can not interact with them).
- Consider exploring the screen by using the `scroll` action with different directions to reveal additional content.
- The direction parameter for the `scroll` action can be confusing sometimes as it's opposite to swipe, for example, to view content at the bottom, the `scroll` direction should be set to "down". It has been observed that you have difficulties in choosing the correct direction, so if one does not work, try the opposite as well.
Text Related Operations:
- Normally to select certain text on the screen: <i> Enter text selection mode by long pressing the area where the text is, then some of the words near the long press point will be selected (highlighted with two pointers indicating the range) and usually a text selection bar will also appear with options like `copy`, `paste`, `select all`, etc. <ii> Select the exact text you need. Usually the text selected from the previous step is NOT the one you want, you need to adjust the range by dragging the two pointers. If you want to select all text in the text field, simply click the `select all` button in the bar.
- At this point, you don't have the ability to drag something around the screen, so in general you can not select arbitrary text.
- To delete some text: the most traditional way is to place the cursor at the right place and use the backspace button in the keyboard to delete the characters one by one (can long press the backspace to accelerate if there are many to delete). Another approach is to first select the text you want to delete, then click the backspace button in the keyboard.
- To copy some text: first select the exact text you want to copy, which usually also brings up the text selection bar, then click the `copy` button in bar.
- To paste text into a text box, first long press the text box, then usually the text selection bar will appear with a `paste` button in it.
- When typing into a text field, sometimes an auto-complete dropdown list will appear. This usually indicating this is a enum field and you should try to select the best match by clicking the corresponding one in the list.
{task_guidelines}
Now output an action from the above list in the correct JSON format, following the reason why you do that. Your answer should look like:
Reason: ...
Action: {{"action_type":...}}

Your Answer:
