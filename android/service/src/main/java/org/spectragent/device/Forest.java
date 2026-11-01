package org.spectragent.device;

import android.accessibilityservice.AccessibilityService;
import android.graphics.Rect;
import android.os.Build;
import android.os.SystemClock;
import android.text.Spanned;
import android.text.style.ClickableSpan;
import android.text.style.URLSpan;
import android.util.JsonWriter;
import android.util.SparseArray;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStreamWriter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

/**
 * The window forest as JSON in android_env's AndroidAccessibilityForest shape (its proto3 JSON
 * form: the proto's field names, enums by name, fields at their default left out), plus one
 * field of ours on each node, {@code is_important}. The layout follows AndroidWorld's
 * accessibility forwarder: displays in order, each display's windows bottom to top, each window's
 * nodes breadth first with ids from 0 at the root and their depth.
 *
 * <p>The tree is the full one: views that are not important for accessibility and view ids come
 * with it (the service's flags). Nodes off the screen are kept as well, up to a limit: once the
 * capture holds maxNodes nodes, a node that is not visible to the user is left out with everything
 * under it, and counted in {@code omitted_offscreen}. Visible nodes are always kept.
 *
 * <p>Before API 33 the framework pools nodes and windows, and each one obtained must be recycled:
 * the capture keeps every one it obtained and recycles them all at its end.
 */
final class Forest {
    private Forest() {}

    private static final class Budget {
        final int maxNodes;
        int nodes;
        int omittedOffscreen;
        /** Every node the capture obtained, recycled at its end before API 33. */
        final List<AccessibilityNodeInfo> obtained = new ArrayList<>();

        Budget(int maxNodes) {
            this.maxNodes = maxNodes;
        }

        AccessibilityNodeInfo keep(AccessibilityNodeInfo node) {
            if (node != null) {
                obtained.add(node);
            }
            return node;
        }
    }

    /** {@code {"forest": {...}, "node_count": n, "omitted_offscreen": k, "capture_ms": t}}. */
    static byte[] capture(AccessibilityService service, int maxNodes) throws IOException {
        long started = SystemClock.uptimeMillis();
        Budget budget = new Budget(maxNodes);
        ByteArrayOutputStream bytes = new ByteArrayOutputStream(256 * 1024);
        JsonWriter json = new JsonWriter(new OutputStreamWriter(bytes, StandardCharsets.UTF_8));
        json.beginObject();
        json.name("forest").beginObject();
        json.name("windows").beginArray();
        SparseArray<List<AccessibilityWindowInfo>> displays = service.getWindowsOnAllDisplays();
        try {
            for (int d = 0; d < displays.size(); d++) {
                List<AccessibilityWindowInfo> windows = displays.valueAt(d);
                for (int w = windows.size() - 1; w >= 0; w--) {
                    window(json, windows.get(w), budget);
                }
            }
        } finally {
            if (Build.VERSION.SDK_INT < 33) {
                for (AccessibilityNodeInfo node : budget.obtained) {
                    node.recycle();
                }
                for (int d = 0; d < displays.size(); d++) {
                    for (AccessibilityWindowInfo window : displays.valueAt(d)) {
                        window.recycle();
                    }
                }
            }
        }
        json.endArray();
        json.endObject();
        json.name("node_count").value(budget.nodes);
        json.name("omitted_offscreen").value(budget.omittedOffscreen);
        json.name("capture_ms").value(SystemClock.uptimeMillis() - started);
        json.endObject();
        json.close();
        return bytes.toByteArray();
    }

    private static void window(JsonWriter json, AccessibilityWindowInfo window, Budget budget) throws IOException {
        Rect bounds = new Rect();
        window.getBoundsInScreen(bounds);
        json.beginObject();
        rect(json, "bounds_in_screen", bounds);
        number(json, "display_id", window.getDisplayId());
        number(json, "id", window.getId());
        number(json, "layer", window.getLayer());
        text(json, "title", window.getTitle());
        String type = windowType(window.getType());
        if (!type.equals("UNKNOWN_TYPE")) {
            json.name("window_type").value(type);
        }
        flag(json, "is_accessibility_focused", window.isAccessibilityFocused());
        flag(json, "is_active", window.isActive());
        flag(json, "is_focused", window.isFocused());
        flag(json, "is_in_picture_in_picture_mode", window.isInPictureInPictureMode());
        json.name("tree").beginObject();
        AccessibilityNodeInfo root = budget.keep(window.getRoot());
        if (root != null) {
            json.name("nodes").beginArray();
            tree(json, root, budget);
            json.endArray();
        }
        json.endObject();
        json.endObject();
    }

    /** One window's nodes, breadth first; a node gets its id when its parent admits it. */
    private static void tree(JsonWriter json, AccessibilityNodeInfo root, Budget budget) throws IOException {
        List<AccessibilityNodeInfo> nodes = new ArrayList<>();
        List<Integer> depths = new ArrayList<>();
        List<int[]> children = new ArrayList<>();
        Map<AccessibilityNodeInfo, Integer> ids = new HashMap<>();
        nodes.add(root);
        depths.add(0);
        ids.put(root, 0);
        budget.nodes++;
        for (int i = 0; i < nodes.size(); i++) {
            AccessibilityNodeInfo node = nodes.get(i);
            int count = node.getChildCount();
            int[] admitted = new int[count];
            int kept = 0;
            for (int c = 0; c < count; c++) {
                AccessibilityNodeInfo child = budget.keep(node.getChild(c));
                if (child == null || ids.containsKey(child)) {
                    continue;
                }
                if (!child.isVisibleToUser() && budget.nodes >= budget.maxNodes) {
                    budget.omittedOffscreen++;
                    continue;
                }
                int id = nodes.size();
                ids.put(child, id);
                nodes.add(child);
                depths.add(depths.get(i) + 1);
                budget.nodes++;
                admitted[kept++] = id;
            }
            children.add(Arrays.copyOf(admitted, kept));
        }
        for (int i = 0; i < nodes.size(); i++) {
            node(json, nodes.get(i), i, depths.get(i), children.get(i), ids, budget);
        }
    }

    /** One node, its fields in the proto's order, then is_important. */
    private static void node(JsonWriter json, AccessibilityNodeInfo node, int id, int depth, int[] childIds,
            Map<AccessibilityNodeInfo, Integer> ids, Budget budget) throws IOException {
        Rect bounds = new Rect();
        node.getBoundsInScreen(bounds);
        json.beginObject();
        number(json, "unique_id", id);
        rect(json, "bounds_in_screen", bounds);
        text(json, "class_name", node.getClassName());
        text(json, "content_description", node.getContentDescription());
        text(json, "hint_text", node.getHintText());
        text(json, "package_name", node.getPackageName());
        text(json, "text", node.getText());
        number(json, "text_selection_start", node.getTextSelectionStart());
        number(json, "text_selection_end", node.getTextSelectionEnd());
        text(json, "view_id_resource_name", node.getViewIdResourceName());
        number(json, "window_id", node.getWindowId());
        flag(json, "is_checkable", node.isCheckable());
        flag(json, "is_checked", node.isChecked());
        flag(json, "is_clickable", node.isClickable());
        flag(json, "is_editable", node.isEditable());
        flag(json, "is_enabled", node.isEnabled());
        flag(json, "is_focusable", node.isFocusable());
        flag(json, "is_focused", node.isFocused());
        flag(json, "is_long_clickable", node.isLongClickable());
        flag(json, "is_password", node.isPassword());
        flag(json, "is_scrollable", node.isScrollable());
        flag(json, "is_selected", node.isSelected());
        flag(json, "is_visible_to_user", node.isVisibleToUser());
        List<AccessibilityNodeInfo.AccessibilityAction> actions = node.getActionList();
        if (!actions.isEmpty()) {
            json.name("actions").beginArray();
            for (AccessibilityNodeInfo.AccessibilityAction action : actions) {
                json.beginObject();
                number(json, "id", action.getId());
                text(json, "label", action.getLabel());
                json.endObject();
            }
            json.endArray();
        }
        if (childIds.length > 0) {
            json.name("child_ids").beginArray();
            for (int child : childIds) {
                json.value(child);
            }
            json.endArray();
        }
        clickableSpans(json, node, id);
        number(json, "depth", depth);
        number(json, "labeled_by_id", idOf(budget.keep(node.getLabeledBy()), ids));
        number(json, "label_for_id", idOf(budget.keep(node.getLabelFor()), ids));
        number(json, "drawing_order", node.getDrawingOrder());
        text(json, "tooltip_text", node.getTooltipText());
        flag(json, "is_important", node.isImportantForAccessibility());
        json.endObject();
    }

    /** The id of a node this capture kept; 0, the proto's "none", for one it did not. */
    private static int idOf(AccessibilityNodeInfo other, Map<AccessibilityNodeInfo, Integer> ids) {
        if (other == null) {
            return 0;
        }
        Integer id = ids.get(other);
        return id == null ? 0 : id;
    }

    private static void clickableSpans(JsonWriter json, AccessibilityNodeInfo node, int id) throws IOException {
        boolean opened = false;
        CharSequence[] sources = {node.getText(), node.getContentDescription()};
        String[] names = {"TEXT", "CONTENT_DESCRIPTION"};
        for (int s = 0; s < sources.length; s++) {
            if (!(sources[s] instanceof Spanned)) {
                continue;
            }
            Spanned spanned = (Spanned) sources[s];
            for (ClickableSpan span : spanned.getSpans(0, spanned.length(), ClickableSpan.class)) {
                if (!opened) {
                    json.name("clickable_spans").beginArray();
                    opened = true;
                }
                int start = spanned.getSpanStart(span);
                int end = spanned.getSpanEnd(span);
                json.beginObject();
                text(json, "text", start >= 0 && end >= start ? spanned.subSequence(start, end) : null);
                text(json, "url", span instanceof URLSpan ? ((URLSpan) span).getURL() : null);
                json.name("source").value(names[s]);
                number(json, "start", start);
                number(json, "node_id", id);
                json.endObject();
            }
        }
        if (opened) {
            json.endArray();
        }
    }

    private static String windowType(int type) {
        switch (type) {
            case AccessibilityWindowInfo.TYPE_APPLICATION:
                return "TYPE_APPLICATION";
            case AccessibilityWindowInfo.TYPE_INPUT_METHOD:
                return "TYPE_INPUT_METHOD";
            case AccessibilityWindowInfo.TYPE_SYSTEM:
                return "TYPE_SYSTEM";
            case AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY:
                return "TYPE_ACCESSIBILITY_OVERLAY";
            case AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER:
                return "TYPE_SPLIT_SCREEN_DIVIDER";
            default:
                return "UNKNOWN_TYPE";
        }
    }

    private static void rect(JsonWriter json, String name, Rect rect) throws IOException {
        if (rect.left == 0 && rect.top == 0 && rect.right == 0 && rect.bottom == 0) {
            return;
        }
        json.name(name).beginObject();
        number(json, "left", rect.left);
        number(json, "top", rect.top);
        number(json, "right", rect.right);
        number(json, "bottom", rect.bottom);
        json.endObject();
    }

    private static void number(JsonWriter json, String name, long value) throws IOException {
        if (value != 0) {
            json.name(name).value(value);
        }
    }

    private static void flag(JsonWriter json, String name, boolean value) throws IOException {
        if (value) {
            json.name(name).value(true);
        }
    }

    private static void text(JsonWriter json, String name, CharSequence value) throws IOException {
        if (value != null && value.length() > 0) {
            json.name(name).value(value.toString());
        }
    }
}
