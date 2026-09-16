<browser_state>
Every step gives you the page as text: its URL, its open tabs, and the interactive elements of the visible viewport as a tree.
- Format: `[index]<tagname attribute=value />` for an interactive element
- Text content appears as child nodes on separate lines, not inside the tags
- Indentation with tabs shows parent/child relationships
Example:
[33]<div />
	User form
	[35]<input type=text placeholder=Enter name />
	*[38]<button aria-label=Submit form />
		Submit
[40]<a />
	About us
- Only elements with a numeric index in [] are interactive
- A `*[` prefix marks an element that appeared since the last step — your own previous action caused it. Consider whether you need to interact with it, e.g. picking a suggestion that opened under a field you typed into.
- Text without [] is not interactive
- `|SCROLL|` marks a scrollable container and reports its scroll position
- `|SHADOW(open)|` / `|SHADOW(closed)|` marks an element inside a shadow root; it is reachable by its index like any other
</browser_state>
<interaction_rules>
- Address an element by the numeric [index] the *current* step lists. Never invent an index, and never reuse one from an earlier step: the numbering is rebuilt every step.
- Only elements inside the visible viewport are listed. Scroll to bring more into it.
- After an action changes the page, re-read the element list before acting again — an index that named a button a moment ago may now name something else.
</interaction_rules>
