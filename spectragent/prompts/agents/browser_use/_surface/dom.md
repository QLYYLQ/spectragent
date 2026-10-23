<browser_state>
Every step gives you the page as text: its URL, its open tabs, and the interactive elements of the visible viewport as a tree.
{dom_format}
</browser_state>
<interaction_rules>
- Address an element by the index the *current* step lists. Never invent an index, and never reuse one from an earlier step: the numbering is rebuilt every step.
- Only elements inside the visible viewport are listed. Scroll to bring more into it.
- After an action changes the page, re-read the element list before acting again — an index that named a button a moment ago may now name something else.
</interaction_rules>
