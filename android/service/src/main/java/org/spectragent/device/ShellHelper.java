package org.spectragent.device;

import android.content.ComponentName;
import android.graphics.Bitmap;
import android.os.SystemClock;
import android.view.InputDevice;
import android.view.KeyCharacterMap;
import android.view.KeyEvent;
import android.view.MotionEvent;
import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.List;
import org.json.JSONObject;

/**
 * The shell helper, run from the APK as the shell user:
 * {@code CLASSPATH=<apk> app_process /system/bin org.spectragent.device.ShellHelper}. Its stdin's
 * first line is {@code <socket name> <token>}; it listens on that abstract socket, prints
 * {@code READY <protocol>} and serves the channel's protocol until its stdin ends, which is when
 * the host's shell connection closes: it never outlives the host's session.
 *
 * <p>Methods: {@code ping}; {@code screenshot} (from SurfaceFlinger, API 31-33, without the
 * service's 333 ms limit); {@code key} (any key, by its KEYCODE_ name); {@code gesture} (one timed
 * stroke, {@link Strokes}, injected with injectInputEvent as {@code input} injects, each event at
 * its planned time); {@code enable_service} and {@code disable_service} (the service appended to
 * enabled_accessibility_services, and taken out again only if this helper put it there; other
 * services kept).
 */
public final class ShellHelper {
    private static final ComponentName SERVICE =
            new ComponentName(BuildConfig.APPLICATION_ID, DeviceService.class.getName());
    private static final String ENABLED = "enabled_accessibility_services";
    /** Moves between a stroke's points, every 8 ms: what `input swipe` sends (120 Hz). */
    private static final long MOVE_INTERVAL_MS = 8;
    /**
     * Whether this helper wrote the service into the setting. Set as soon as the write is made, so
     * that disable_service takes it out again even when the enable failed afterwards, or its answer
     * never reached the host; a service enabled before the run is never this helper's to remove.
     */
    private static boolean added;

    private ShellHelper() {}

    public static void main(String[] args) {
        int status = 0;
        try {
            run();
        } catch (Throwable failure) {
            System.err.println("spectragent helper: " + failure);
            failure.printStackTrace();
            status = 1;
        }
        System.exit(status);
    }

    private static void run() throws IOException {
        BufferedReader stdin = new BufferedReader(new InputStreamReader(System.in, StandardCharsets.UTF_8));
        String line = stdin.readLine();
        String[] words = line == null ? new String[0] : line.trim().split(" ");
        if (words.length != 2) {
            throw new IllegalArgumentException("the first line on stdin is '<socket name> <token>'");
        }
        String token = words[1];
        Server server = new Server(words[0], ShellHelper::handle, null, () -> token,
                Identity.hello("helper", System.getProperty("java.class.path"), null));
        server.start();
        System.out.println("READY " + BuildConfig.PROTOCOL_VERSION);
        System.out.flush();
        while (stdin.read() != -1) {
            // The host writes nothing more: the end of stdin is the end of its session.
        }
        server.stop();
    }

    private static Reply handle(String method, JSONObject params) throws Failure {
        switch (method) {
            case "ping":
                return Reply.of(Json.obj("uptime_ms", SystemClock.uptimeMillis()));
            case "screenshot":
                return screenshot();
            case "key":
                return key(params);
            case "gesture":
                return Reply.of(Json.obj("elapsed_ms", touch(Strokes.parse(params, 1).get(0))));
            case "enable_service":
                return enableService();
            case "disable_service":
                return disableService();
            default:
                throw new Failure("unknown_method", "the helper has no method " + method);
        }
    }

    private static Reply screenshot() throws Failure {
        long started = SystemClock.uptimeMillis();
        Bitmap bitmap = Hidden.captureDisplay();
        long captured = SystemClock.uptimeMillis();
        int width = bitmap.getWidth();
        int height = bitmap.getHeight();
        byte[] png = Images.png(bitmap);
        bitmap.recycle();
        return Reply.of(Json.obj(
                "width", width,
                "height", height,
                "format", "png",
                "capture_ms", captured - started,
                "encode_ms", SystemClock.uptimeMillis() - captured), png);
    }

    private static Reply key(JSONObject params) throws Failure {
        String name = params.optString("code");
        int code = name.startsWith("KEYCODE_") ? KeyEvent.keyCodeFromString(name) : KeyEvent.KEYCODE_UNKNOWN;
        if (code == KeyEvent.KEYCODE_UNKNOWN) {
            throw new Failure("bad_params", "not an Android key code: " + name);
        }
        long down = SystemClock.uptimeMillis();
        injectKey(down, down, KeyEvent.ACTION_DOWN, code);
        injectKey(down, SystemClock.uptimeMillis(), KeyEvent.ACTION_UP, code);
        return Reply.of(new JSONObject());
    }

    private static void injectKey(long down, long time, int action, int code) throws Failure {
        Hidden.inject(new KeyEvent(down, time, action, code, 0, 0, KeyCharacterMap.VIRTUAL_KEYBOARD, 0, 0,
                InputDevice.SOURCE_KEYBOARD), Hidden.INJECT_WAIT_FOR_FINISH);
    }

    /**
     * One finger along timed points: down at the first point's time, up at the last's. Between two
     * points that differ it moves every {@link #MOVE_INTERVAL_MS}, on the straight line, and at each
     * point itself; it sends nothing while the finger holds still, so a tap is a down and an up.
     * Each event carries its planned time and is injected once that time has come, without waiting
     * for the app to handle it: an app takes input once a frame, and waiting for each move held a
     * 350 ms path of 44 moves to about 750 ms (measured). The up waits, so this returns once the app
     * has handled the whole stroke.
     */
    private static long touch(float[][] points) throws Failure {
        int device = touchscreen();
        long origin = SystemClock.uptimeMillis();
        long down = origin + (long) points[0][2];
        sleepUntil(down);
        injectMotion(down, down, MotionEvent.ACTION_DOWN, points[0][0], points[0][1], device);
        for (int i = 1; i < points.length; i++) {
            float[] from = points[i - 1];
            float[] to = points[i];
            long start = origin + (long) from[2];
            long end = origin + (long) to[2];
            if (from[0] != to[0] || from[1] != to[1]) {
                for (long at = start + MOVE_INTERVAL_MS; at < end; at += MOVE_INTERVAL_MS) {
                    float done = (float) (at - start) / (end - start);
                    sleepUntil(at);
                    injectMotion(down, at, MotionEvent.ACTION_MOVE,
                            from[0] + (to[0] - from[0]) * done, from[1] + (to[1] - from[1]) * done, device);
                }
            }
            boolean last = i == points.length - 1;
            if (last || from[0] != to[0] || from[1] != to[1]) {
                sleepUntil(end);
                injectMotion(down, end, last ? MotionEvent.ACTION_UP : MotionEvent.ACTION_MOVE, to[0], to[1], device);
            }
        }
        return SystemClock.uptimeMillis() - origin;
    }

    private static void sleepUntil(long uptimeMs) {
        long wait = uptimeMs - SystemClock.uptimeMillis();
        if (wait > 0) {
            SystemClock.sleep(wait);
        }
    }

    private static void injectMotion(long down, long time, int action, float x, float y, int device) throws Failure {
        int mode = action == MotionEvent.ACTION_UP ? Hidden.INJECT_WAIT_FOR_FINISH : Hidden.INJECT_ASYNC;
        MotionEvent.PointerProperties properties = new MotionEvent.PointerProperties();
        properties.id = 0;
        properties.toolType = MotionEvent.TOOL_TYPE_FINGER;
        MotionEvent.PointerCoords coords = new MotionEvent.PointerCoords();
        coords.x = x;
        coords.y = y;
        coords.pressure = 1f;
        coords.size = 1f;
        MotionEvent event = MotionEvent.obtain(down, time, action, 1,
                new MotionEvent.PointerProperties[] {properties}, new MotionEvent.PointerCoords[] {coords},
                0, 0, 1f, 1f, device, 0, InputDevice.SOURCE_TOUCHSCREEN, 0);
        try {
            Hidden.inject(event, mode);
        } finally {
            event.recycle();
        }
    }

    /** The first touchscreen's device id, as `input` picks it; 0 without one. */
    private static int touchscreen() {
        for (int id : InputDevice.getDeviceIds()) {
            InputDevice device = InputDevice.getDevice(id);
            if (device != null && device.supportsSource(InputDevice.SOURCE_TOUCHSCREEN)) {
                return id;
            }
        }
        return 0;
    }

    private static synchronized Reply enableService() throws Failure {
        String previous = setting(ENABLED);
        List<String> entries = entries(previous);
        boolean present = false;
        for (String entry : entries) {
            present |= SERVICE.equals(ComponentName.unflattenFromString(entry));
        }
        String current = previous;
        if (!present) {
            entries.add(SERVICE.flattenToShortString());
            current = String.join(":", entries);
            settings("put", "secure", ENABLED, current);
            added = true;
        }
        settings("put", "secure", "accessibility_enabled", "1");
        return Reply.of(Json.obj("previous", previous, "current", current, "changed", !present));
    }

    private static synchronized Reply disableService() throws Failure {
        String previous = setting(ENABLED);
        if (!added) {
            return Reply.of(Json.obj("previous", previous, "current", previous, "changed", false));
        }
        List<String> entries = entries(previous);
        List<String> kept = new ArrayList<>();
        for (String entry : entries) {
            if (!SERVICE.equals(ComponentName.unflattenFromString(entry))) {
                kept.add(entry);
            }
        }
        boolean changed = kept.size() != entries.size();
        String current = previous;
        if (changed && kept.isEmpty()) {
            settings("delete", "secure", ENABLED);
            current = null;
        } else if (changed) {
            current = String.join(":", kept);
            settings("put", "secure", ENABLED, current);
        }
        added = false;
        return Reply.of(Json.obj("previous", previous, "current", current, "changed", changed));
    }

    /** A secure setting's value; null when it is unset or empty. */
    private static String setting(String name) throws Failure {
        String value = settings("get", "secure", name).trim();
        return value.isEmpty() || value.equals("null") ? null : value;
    }

    private static List<String> entries(String value) {
        List<String> entries = new ArrayList<>();
        if (value != null) {
            for (String entry : value.split(":")) {
                if (!entry.isEmpty()) {
                    entries.add(entry);
                }
            }
        }
        return entries;
    }

    /** Runs the settings command, as the shell user may write secure settings. */
    private static String settings(String... args) throws Failure {
        String[] command = new String[args.length + 1];
        command[0] = "/system/bin/settings";
        System.arraycopy(args, 0, command, 1, args.length);
        try {
            Process process = new ProcessBuilder(command).redirectErrorStream(true).start();
            String output = readAll(process.getInputStream());
            int exit = process.waitFor();
            if (exit != 0) {
                throw new Failure("settings_failed", "settings " + String.join(" ", args) + " exited " + exit + ": "
                        + output.trim());
            }
            return output;
        } catch (IOException failed) {
            throw new Failure("settings_failed", "settings " + String.join(" ", args) + ": " + failed);
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            throw new Failure("interrupted", "the helper is stopping");
        }
    }

    private static String readAll(InputStream in) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buffer = new byte[4096];
        int read;
        while ((read = in.read(buffer)) != -1) {
            out.write(buffer, 0, read);
        }
        return out.toString(StandardCharsets.UTF_8.name());
    }
}
