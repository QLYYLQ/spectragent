package org.spectragent.device;

import android.graphics.Bitmap;
import android.os.Build;
import android.os.IBinder;
import android.view.InputEvent;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;

/**
 * The hidden framework APIs the shell helper calls by reflection, as the shell user may, each on
 * the API levels it was written for. Never UiAutomation: the default one unbinds every
 * accessibility service while it is connected (docs/architecture_design.md §11.2).
 */
final class Hidden {
    /** Queue the event and return at once. */
    static final int INJECT_ASYNC = 0;
    /** Return once the window the event went to has handled it. */
    static final int INJECT_WAIT_FOR_FINISH = 2;
    private static Object inputManager;
    private static Method injectInputEvent;

    private Hidden() {}

    /**
     * The default display now, straight from SurfaceFlinger: SurfaceControl.captureDisplay
     * (API 31-33; Android 14 moved it to ScreenCapture). A hardware bitmap.
     */
    static Bitmap captureDisplay() throws Failure {
        if (Build.VERSION.SDK_INT < 31 || Build.VERSION.SDK_INT > 33) {
            throw new Failure("unsupported", "the helper's screenshot is written for API 31 to 33; this device runs API "
                    + Build.VERSION.SDK_INT);
        }
        try {
            Class<?> surfaceControl = Class.forName("android.view.SurfaceControl");
            Class<?> argsClass = Class.forName("android.view.SurfaceControl$DisplayCaptureArgs");
            Class<?> builderClass = Class.forName("android.view.SurfaceControl$DisplayCaptureArgs$Builder");
            Object builder = builderClass.getConstructor(IBinder.class).newInstance(displayToken(surfaceControl));
            Object captureArgs = builderClass.getMethod("build").invoke(builder);
            Object buffer = surfaceControl.getMethod("captureDisplay", argsClass).invoke(null, captureArgs);
            if (buffer == null) {
                throw new Failure("screenshot_failed", "SurfaceControl.captureDisplay returned nothing");
            }
            return (Bitmap) buffer.getClass().getMethod("asBitmap").invoke(buffer);
        } catch (ReflectiveOperationException failed) {
            throw new Failure("screenshot_failed", "SurfaceControl.captureDisplay: " + cause(failed));
        }
    }

    private static IBinder displayToken(Class<?> surfaceControl) throws ReflectiveOperationException {
        try {
            return (IBinder) surfaceControl.getMethod("getInternalDisplayToken").invoke(null);
        } catch (NoSuchMethodException removed) {
            long[] ids = (long[]) surfaceControl.getMethod("getPhysicalDisplayIds").invoke(null);
            return (IBinder) surfaceControl.getMethod("getPhysicalDisplayToken", long.class).invoke(null, ids[0]);
        }
    }

    /**
     * Injects event in mode: {@link #INJECT_ASYNC}, or {@link #INJECT_WAIT_FOR_FINISH} as `input`
     * injects. Events are dispatched in the order they were injected either way.
     */
    static synchronized void inject(InputEvent event, int mode) throws Failure {
        try {
            if (injectInputEvent == null) {
                Class<?> managerClass;
                try { // Android 14 moved it
                    managerClass = Class.forName("android.hardware.input.InputManagerGlobal");
                } catch (ClassNotFoundException olderThan14) {
                    managerClass = Class.forName("android.hardware.input.InputManager");
                }
                inputManager = managerClass.getMethod("getInstance").invoke(null);
                injectInputEvent = managerClass.getMethod("injectInputEvent", InputEvent.class, int.class);
            }
            Object injected = injectInputEvent.invoke(inputManager, event, mode);
            if (!Boolean.TRUE.equals(injected)) {
                throw new Failure("inject_failed", "the input manager did not inject " + event);
            }
        } catch (ReflectiveOperationException failed) {
            throw new Failure("inject_failed", "InputManager.injectInputEvent: " + cause(failed));
        }
    }

    private static String cause(ReflectiveOperationException failed) {
        Throwable cause = failed instanceof InvocationTargetException ? failed.getCause() : failed;
        return String.valueOf(cause);
    }
}
