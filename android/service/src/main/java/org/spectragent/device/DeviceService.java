package org.spectragent.device;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.GestureDescription;
import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Path;
import android.hardware.HardwareBuffer;
import android.os.Build;
import android.os.Bundle;
import android.os.SystemClock;
import android.util.Log;
import android.view.Display;
import android.view.accessibility.AccessibilityEvent;
import android.view.accessibility.AccessibilityNodeInfo;
import java.io.IOException;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import org.json.JSONArray;
import org.json.JSONObject;

/**
 * The accessibility service. It listens on {@link #SOCKET} only while the system has it bound,
 * so an answered ping proves the binding (docs/architecture_design.md §11.2).
 *
 * <p>Methods: {@code ping}; {@code tree} (the window forest, {@link Forest}); {@code screenshot}
 * (API 30+, PNG, at most one per {@link #SCREENSHOT_INTERVAL_MS}: a request sooner waits, and one
 * the system still finds too soon is sent again until {@link #SCREENSHOT_TIMEOUT_MS});
 * {@code gesture} (timed strokes, {@link Strokes}, up to the system's limits); {@code global_action} (back,
 * home, recents); {@code set_text} (ACTION_SET_TEXT on the field with input focus) and
 * {@code commit_text} (API 33, through the service's own input method, at the cursor); and
 * {@code subscribe}, after which every accessibility event is pushed.
 */
public final class DeviceService extends AccessibilityService {
    static final String SOCKET = "spectragent_device";
    private static final String TAG = "Spectragent";
    /**
     * The system refuses a screenshot sooner than this after the last one: 1000 ms on API 30 and
     * 333 ms from API 31 (AccessibilityService's request interval); a little more, to be sure.
     */
    private static final long SCREENSHOT_INTERVAL_MS = Build.VERSION.SDK_INT >= 31 ? 340 : 1010;
    /** How long a screenshot the system found too soon waits before it is asked again. */
    private static final long SCREENSHOT_RETRY_MS = 50;
    private static final long SCREENSHOT_TIMEOUT_MS = 10_000;
    /** How long past its own duration a gesture may take to be reported done. */
    private static final long GESTURE_GRACE_MS = 5_000;

    private final EventHub events = new EventHub();
    private final Object screenshotTurn = new Object();
    private long lastScreenshotAt = -SCREENSHOT_INTERVAL_MS;
    private Server server;

    @Override
    protected void onServiceConnected() {
        Boolean tool = Build.VERSION.SDK_INT >= 31 ? getServiceInfo().isAccessibilityTool() : null;
        Server started = new Server(SOCKET, this::handle, events, Session::token,
                Identity.hello("service", getApplicationInfo().sourceDir, tool));
        try {
            started.start();
            server = started;
            Session.service(true, true);
        } catch (IOException taken) {
            Log.e(TAG, "cannot listen on " + SOCKET, taken);
            Session.service(true, false);
        }
    }

    @Override
    public boolean onUnbind(Intent intent) {
        stop();
        return false;
    }

    @Override
    public void onDestroy() {
        stop();
        super.onDestroy();
    }

    @Override
    public void onInterrupt() {}

    @Override
    public void onAccessibilityEvent(AccessibilityEvent event) {
        // Encoded right here: the system recycles the event once this returns.
        events.publish(seq -> Frames.encode(event(seq, event)));
    }

    private void stop() {
        Server running = server;
        server = null;
        if (running != null) {
            running.stop();
        }
        Session.service(false, false);
    }

    private Reply handle(String method, JSONObject params) throws Failure {
        switch (method) {
            case "ping":
                return Reply.of(Json.obj("uptime_ms", SystemClock.uptimeMillis(), "next_seq", events.nextSeq()));
            case "tree":
                return tree(params);
            case "screenshot":
                return screenshot();
            case "gesture":
                return gesture(params);
            case "global_action":
                return globalAction(params);
            case "set_text":
                return setText(params);
            case "commit_text":
                return commitText(params);
            default:
                throw new Failure("unknown_method", "the service has no method " + method);
        }
    }

    private Reply tree(JSONObject params) throws Failure {
        int maxNodes = params.optInt("max_nodes", -1);
        if (maxNodes < 1) {
            throw new Failure("bad_params", "max_nodes is a positive number of nodes");
        }
        try {
            return new Reply(Forest.capture(this, maxNodes), Frames.EMPTY);
        } catch (IOException impossible) { // written to memory
            throw new IllegalStateException(impossible);
        }
    }

    private Reply screenshot() throws Failure {
        synchronized (screenshotTurn) {
            long waited = Math.max(0, lastScreenshotAt + SCREENSHOT_INTERVAL_MS - SystemClock.uptimeMillis());
            sleep(waited);
            long started = SystemClock.uptimeMillis();
            ScreenshotResult shot = takeScreenshotNow();
            while (shot.error == ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT
                    && SystemClock.uptimeMillis() - started < SCREENSHOT_TIMEOUT_MS) {
                sleep(SCREENSHOT_RETRY_MS);
                waited += SCREENSHOT_RETRY_MS;
                shot = takeScreenshotNow();
            }
            if (shot.error != 0) {
                throw new Failure("screenshot_failed", "takeScreenshot failed with error " + shot.error);
            }
            HardwareBuffer buffer = shot.result.getHardwareBuffer();
            try {
                Bitmap bitmap = Bitmap.wrapHardwareBuffer(buffer, shot.result.getColorSpace());
                if (bitmap == null) {
                    throw new Failure("screenshot_failed", "the screenshot's buffer could not be read");
                }
                long captured = SystemClock.uptimeMillis();
                int width = bitmap.getWidth();
                int height = bitmap.getHeight();
                byte[] png = Images.png(bitmap);
                bitmap.recycle();
                return Reply.of(Json.obj(
                        "width", width,
                        "height", height,
                        "format", "png",
                        "waited_ms", waited,
                        "capture_ms", captured - started,
                        "encode_ms", SystemClock.uptimeMillis() - captured), png);
            } finally {
                buffer.close();
            }
        }
    }

    private static final class ScreenshotResult {
        AccessibilityService.ScreenshotResult result;
        int error;
    }

    private ScreenshotResult takeScreenshotNow() throws Failure {
        ScreenshotResult shot = new ScreenshotResult();
        CountDownLatch done = new CountDownLatch(1);
        lastScreenshotAt = SystemClock.uptimeMillis();
        takeScreenshot(Display.DEFAULT_DISPLAY, Runnable::run, new TakeScreenshotCallback() {
            @Override
            public void onSuccess(AccessibilityService.ScreenshotResult result) {
                shot.result = result;
                done.countDown();
            }

            @Override
            public void onFailure(int errorCode) {
                shot.error = errorCode;
                done.countDown();
            }
        });
        await(done, SCREENSHOT_TIMEOUT_MS, "screenshot_timeout", "no screenshot within " + SCREENSHOT_TIMEOUT_MS + " ms");
        return shot;
    }

    /**
     * Timed strokes ({@link Strokes}). When every stroke has two points, one dispatch carries them
     * all, each finger moving at one speed from its first point to its second. A stroke of more
     * points (one finger only) is chained: each segment is its own dispatch, continuing the stroke
     * before it ({@code continueStroke}), so each runs at its own speed; a segment is dispatched once
     * the system reports the one before it done, so the path also takes those round trips.
     */
    private Reply gesture(JSONObject params) throws Failure {
        List<float[][]> strokes = Strokes.parse(params, GestureDescription.getMaxStrokeCount());
        boolean straight = true;
        for (float[][] stroke : strokes) {
            straight &= stroke.length == 2;
        }
        if (!straight && strokes.size() > 1) {
            throw new Failure("bad_params", "the service times a path of more than two points for one finger only");
        }
        long started = SystemClock.uptimeMillis();
        if (straight) {
            GestureDescription.Builder builder = new GestureDescription.Builder();
            long end = 0;
            for (float[][] stroke : strokes) {
                long start = (long) stroke[0][2];
                long duration = Math.max(1, (long) stroke[1][2] - start);
                builder.addStroke(new GestureDescription.StrokeDescription(segment(stroke[0], stroke[1]), start, duration));
                end = Math.max(end, start + duration);
            }
            if (end > GestureDescription.getMaxGestureDuration()) {
                throw new Failure("bad_params", "a gesture lasts at most " + GestureDescription.getMaxGestureDuration() + " ms");
            }
            dispatch(builder.build(), end);
        } else {
            float[][] points = strokes.get(0);
            GestureDescription.StrokeDescription previous = null;
            for (int i = 1; i < points.length; i++) {
                long start = previous == null ? (long) points[0][2] : 0;
                long duration = Math.max(1, (long) points[i][2] - (long) points[i - 1][2]);
                boolean more = i < points.length - 1;
                Path path = segment(points[i - 1], points[i]);
                GestureDescription.StrokeDescription stroke = previous == null
                        ? new GestureDescription.StrokeDescription(path, start, duration, more)
                        : previous.continueStroke(path, start, duration, more);
                dispatch(new GestureDescription.Builder().addStroke(stroke).build(), start + duration);
                previous = stroke;
            }
        }
        return Reply.of(Json.obj("elapsed_ms", SystemClock.uptimeMillis() - started));
    }

    private static Path segment(float[] from, float[] to) {
        Path path = new Path();
        path.moveTo(from[0], from[1]);
        path.lineTo(to[0], to[1]);
        return path;
    }

    /** Dispatches gesture and returns once the system completed it, within its length and a grace. */
    private void dispatch(GestureDescription gesture, long lengthMs) throws Failure {
        CountDownLatch done = new CountDownLatch(1);
        boolean[] completed = {false};
        boolean dispatched = dispatchGesture(gesture, new GestureResultCallback() {
            @Override
            public void onCompleted(GestureDescription description) {
                completed[0] = true;
                done.countDown();
            }

            @Override
            public void onCancelled(GestureDescription description) {
                done.countDown();
            }
        }, null);
        if (!dispatched) {
            throw new Failure("gesture_refused", "the system refused to dispatch the gesture");
        }
        await(done, lengthMs + GESTURE_GRACE_MS, "gesture_timeout", "the gesture was not reported done");
        if (!completed[0]) {
            throw new Failure("gesture_cancelled", "the system cancelled the gesture; it cancels every gesture on a "
                    + "device without android.hardware.faketouch, and one that another gesture interrupts");
        }
    }

    private Reply globalAction(JSONObject params) throws Failure {
        String action = params.optString("action");
        int id;
        switch (action) {
            case "back":
                id = GLOBAL_ACTION_BACK;
                break;
            case "home":
                id = GLOBAL_ACTION_HOME;
                break;
            case "recents":
                id = GLOBAL_ACTION_RECENTS;
                break;
            default:
                throw new Failure("bad_params", "action is back, home or recents, not " + action);
        }
        if (!performGlobalAction(id)) {
            throw new Failure("global_action_failed", "the system did not perform " + action);
        }
        return Reply.of(new JSONObject());
    }

    private Reply setText(JSONObject params) throws Failure {
        String text = text(params);
        AccessibilityNodeInfo field = findFocus(AccessibilityNodeInfo.FOCUS_INPUT);
        try {
            if (field == null || !field.isEditable()) {
                throw new Failure("no_focused_field", "no editable field has input focus");
            }
            Bundle arguments = new Bundle();
            arguments.putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text);
            if (!field.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)) {
                throw new Failure("set_text_failed", "the focused field refused the text");
            }
            return Reply.of(Json.obj(
                    "package_name", string(field.getPackageName()),
                    "class_name", string(field.getClassName()),
                    "view_id_resource_name", field.getViewIdResourceName()));
        } finally {
            if (field != null && Build.VERSION.SDK_INT < 33) {
                field.recycle(); // pooled before API 33
            }
        }
    }

    private Reply commitText(JSONObject params) throws Failure {
        String text = text(params);
        if (Build.VERSION.SDK_INT < 33) {
            throw new Failure("unsupported", "commit_text needs Android 13 (API 33); this device runs API " + Build.VERSION.SDK_INT);
        }
        return Reply.of(Api33.commitText(this, text));
    }

    private static String text(JSONObject params) throws Failure {
        if (!params.has("text")) {
            throw new Failure("bad_params", "text is required");
        }
        return params.optString("text");
    }

    /** One event as an event frame's header. */
    private static JSONObject event(long seq, AccessibilityEvent event) {
        JSONArray text = new JSONArray();
        List<CharSequence> parts = event.getText();
        for (CharSequence part : parts) {
            text.put(string(part));
        }
        return Json.obj(
                "kind", "event",
                "seq", seq,
                "type", AccessibilityEvent.eventTypeToString(event.getEventType()),
                "time_ms", event.getEventTime(),
                "package_name", string(event.getPackageName()),
                "class_name", string(event.getClassName()),
                "window_id", event.getWindowId(),
                "content_change_types", event.getContentChangeTypes(),
                "window_changes", event.getWindowChanges(),
                "text", text,
                "content_description", string(event.getContentDescription()),
                "before_text", string(event.getBeforeText()),
                "from_index", event.getFromIndex(),
                "to_index", event.getToIndex(),
                "item_count", event.getItemCount(),
                "added_count", event.getAddedCount(),
                "removed_count", event.getRemovedCount(),
                "scroll_delta_x", event.getScrollDeltaX(),
                "scroll_delta_y", event.getScrollDeltaY());
    }

    private static String string(CharSequence value) {
        return value == null ? null : value.toString();
    }

    private static void sleep(long ms) throws Failure {
        if (ms <= 0) {
            return;
        }
        try {
            Thread.sleep(ms);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            throw new Failure("interrupted", "the service is stopping");
        }
    }

    private static void await(CountDownLatch done, long ms, String code, String message) throws Failure {
        try {
            if (!done.await(ms, TimeUnit.MILLISECONDS)) {
                throw new Failure(code, message);
            }
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            throw new Failure("interrupted", "the service is stopping");
        }
    }
}
