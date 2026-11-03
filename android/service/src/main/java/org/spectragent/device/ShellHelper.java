package org.spectragent.device;

import android.content.ComponentName;
import android.graphics.Bitmap;
import android.os.SystemClock;
import android.view.InputDevice;
import android.view.InputEvent;
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
import java.util.Collections;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Set;
import org.json.JSONObject;

/**
 * The shell helper, run from the APK as the shell user:
 * {@code CLASSPATH=<apk> app_process /system/bin org.spectragent.device.ShellHelper}. Its stdin's
 * first line is {@code <socket name> <token>}; it listens on that abstract socket, prints
 * {@code READY <protocol>} and serves the channel's protocol until its stdin ends, which is when
 * the host's shell connection closes: it never outlives the host's session.
 *
 * <p>Methods: {@code ping}; {@code screenshot} (from SurfaceFlinger, API 31-33, without the
 * service's 333 ms limit); {@code key} (any key, by its KEYCODE_ name); {@code type_text} (text as
 * key events, mapped as {@code input text} maps it, see {@link #typeText}); {@code gesture} (one
 * timed stroke, {@link Strokes}, injected with injectInputEvent as {@code input} injects, each
 * event at its planned time); {@code enable_service} and {@code disable_service} (the service
 * appended to enabled_accessibility_services, and taken out again only if this helper put it
 * there; other services kept).
 */
public final class ShellHelper {
    private static final ComponentName SERVICE =
            new ComponentName(BuildConfig.APPLICATION_ID, DeviceService.class.getName());
    private static final String ENABLED = "enabled_accessibility_services";
    /** Moves between a stroke's points, every 8 ms: what `input swipe` sends (120 Hz). */
    private static final long MOVE_INTERVAL_MS = 8;
    /** How many characters without a key a refused type_text names; the rest it counts. */
    private static final int MAX_NAMED_CHARACTERS = 10;
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
            case "type_text":
                return typeText(params, Hidden::inject);
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
        Hidden.Capture capture = Hidden.captureDisplay();
        long captured = SystemClock.uptimeMillis();
        int width = capture.bitmap.getWidth();
        int height = capture.bitmap.getHeight();
        byte[] png;
        try {
            png = Images.png(capture.bitmap);
        } finally {
            capture.close();
        }
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
        Keys keys = new Keys(Hidden::inject);
        try {
            keys.send(KeyEvent.ACTION_DOWN, code, 0);
            keys.send(KeyEvent.ACTION_UP, code, 0);
        } catch (Failure failed) {
            keys.liftAll();
            throw failed;
        }
        return Reply.of(new JSONObject());
    }

    /**
     * Text typed as key events, as {@code input text} types it: the virtual keyboard's key map
     * (KeyCharacterMap.VIRTUAL_KEYBOARD) gives each character the keys a keyboard presses for it,
     * shift with a capital and ENTER for a newline, and each event is injected and waited for. A
     * character with no key (see {@link #keyless}) fails the request with unmappable_text, naming
     * it, before any key is pressed; {@code input text} throws there, and AndroidWorld drops such
     * characters before it calls it. With {@code "replace": true}, ctrl+A and DEL come first,
     * selecting the field's text and deleting it, as AndroidWorld clears a field. Each event is
     * stamped as it is injected: the input dispatcher drops an event more than 10 s old, which a
     * long text reaches when every event carries the time the text was mapped, as
     * {@code input text}'s do.
     *
     * <p>A key that fails to go in lifts every key still down ({@link Keys#liftAll}), so no shift or
     * ctrl stays held, and fails the request: with the injector's own failure when it was the first
     * key, so nothing went in, else with typed_partly, since the clear and the keys before it did.
     */
    static Reply typeText(JSONObject params, Injector injector) throws Failure {
        if (!params.has("text")) {
            throw new Failure("bad_params", "text is required");
        }
        String text = params.optString("text");
        KeyCharacterMap map = KeyCharacterMap.load(KeyCharacterMap.VIRTUAL_KEYBOARD);
        String keyless = keyless(map, text);
        if (!keyless.isEmpty()) {
            throw new Failure("unmappable_text", "the device's key map has no key for " + keyless
                    + ", so no key was pressed");
        }
        KeyEvent[] events = text.isEmpty() ? new KeyEvent[0] : map.getEvents(text.toCharArray());
        Keys keys = new Keys(injector);
        try {
            if (params.optBoolean("replace", false)) {
                int ctrl = KeyEvent.META_CTRL_ON | KeyEvent.META_CTRL_LEFT_ON;
                keys.send(KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_CTRL_LEFT, ctrl);
                keys.send(KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_A, ctrl);
                keys.send(KeyEvent.ACTION_UP, KeyEvent.KEYCODE_A, ctrl);
                keys.send(KeyEvent.ACTION_UP, KeyEvent.KEYCODE_CTRL_LEFT, 0);
                keys.send(KeyEvent.ACTION_DOWN, KeyEvent.KEYCODE_DEL, 0);
                keys.send(KeyEvent.ACTION_UP, KeyEvent.KEYCODE_DEL, 0);
            }
            for (KeyEvent event : events) {
                keys.send(event.getAction(), event.getKeyCode(), event.getMetaState());
            }
        } catch (Failure failed) {
            keys.liftAll();
            if (keys.sent == 0) {
                throw failed;
            }
            throw new Failure("typed_partly", keys.sent + (keys.sent == 1 ? " key event" : " key events")
                    + " went in before one failed: " + failed.getMessage());
        }
        return Reply.of(new JSONObject());
    }

    /**
     * The characters of text with no key, each once, as its code point and, when it shows on its
     * own, itself; empty when every character has keys. The map has no key for most (Chinese, an
     * accented letter). The combining accents it reaches with Alt (U+0300 grave) are dead keys,
     * which put the accent on the character typed after them, not on the one before, so a
     * combining mark counts as having no key: an e followed by U+0301 would come out wrong.
     */
    private static String keyless(KeyCharacterMap keys, String text) {
        List<String> found = new ArrayList<>();
        Set<Integer> seen = new HashSet<>();
        int more = 0;
        for (int i = 0; i < text.length(); i += Character.charCount(text.codePointAt(i))) {
            int point = text.codePointAt(i);
            boolean combining = Character.getType(point) == Character.NON_SPACING_MARK;
            if (!seen.add(point) || (!combining && keys.getEvents(Character.toChars(point)) != null)) {
                continue;
            }
            if (found.size() == MAX_NAMED_CHARACTERS) {
                more++;
                continue;
            }
            String code = String.format(Locale.ROOT, "U+%04X", point);
            boolean shows = !combining && !Character.isISOControl(point);
            found.add(shows ? "'" + new String(Character.toChars(point)) + "' (" + code + ")" : code);
        }
        return String.join(", ", found) + (more > 0 ? " and " + more + " more" : "");
    }

    /** Injects one input event, waiting until it is handled or not (Hidden's modes); a test stands in for it. */
    interface Injector {
        void inject(InputEvent event, int mode) throws Failure;
    }

    /**
     * The key events of one request, through one injector: how many went in, and which keys are
     * down, each with its down time, so an up carries the down time of its own down.
     */
    private static final class Keys {
        private final Injector injector;
        /** Keys pressed and not yet released, in the order pressed. */
        private final LinkedHashMap<Integer, Long> down = new LinkedHashMap<>();
        /** How many events went in. */
        int sent;

        Keys(Injector injector) {
            this.injector = injector;
        }

        /**
         * Sends one key event, stamped now, and waits until the window handled it. A down counts as
         * held from the moment it is tried: one that timed out may still arrive.
         */
        void send(int action, int code, int metaState) throws Failure {
            long now = SystemClock.uptimeMillis();
            if (action == KeyEvent.ACTION_DOWN) {
                down.put(code, now);
            }
            Long pressed = down.get(code);
            injector.inject(keyEvent(pressed == null ? now : pressed, now, action, code, metaState),
                    Hidden.INJECT_WAIT_FOR_FINISH);
            if (action == KeyEvent.ACTION_UP) {
                down.remove(code);
            }
            sent++;
        }

        /** Lifts every key still down, the last pressed first, without waiting: the window may not answer. */
        void liftAll() {
            List<Integer> codes = new ArrayList<>(down.keySet());
            Collections.reverse(codes);
            for (int code : codes) {
                try {
                    injector.inject(keyEvent(down.get(code), SystemClock.uptimeMillis(), KeyEvent.ACTION_UP, code, 0),
                            Hidden.INJECT_ASYNC);
                } catch (Failure ignored) {
                    // The request already fails with the first failure.
                }
            }
            down.clear();
        }
    }

    private static KeyEvent keyEvent(long down, long time, int action, int code, int metaState) {
        return new KeyEvent(down, time, action, code, 0, metaState, KeyCharacterMap.VIRTUAL_KEYBOARD, 0, 0,
                InputDevice.SOURCE_KEYBOARD);
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
