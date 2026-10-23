<browser_state>
Every step gives you the page as text: its URL, its open tabs, and the interactive elements of the visible viewport as a tree.
{dom_format}
</browser_state>
<browser_vision>
A screenshot of the visible viewport is attached to every step. It is your ground truth for what the page actually looks like: judge success or failure from it first, and fall back to the element list when the image is not decisive. The two describe the same viewport — the image shows you what is there, the element list tells you what you can address by index.
</browser_vision>
<interaction_rules>
- Address an element by an index the *current* step lists. Never invent an index. An element keeps its index from one step to the next for as long as it stays on the page — indexes are not renumbered between steps — but an element this step does not list cannot be reached, not even by the index it had before.
- Only elements inside the visible viewport are listed. Scroll to bring more into it.
- After an action changes the page, re-read the element list before acting again — an element that was replaced or removed is gone with its index, even when something that looks the same took its place.
- Something visible in the screenshot but carrying no index cannot be clicked by index. If this step offers a coordinate action, aim at it in the image instead; otherwise scroll it into a state where it is listed.
</interaction_rules>
