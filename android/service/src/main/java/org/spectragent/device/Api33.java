package org.spectragent.device;

import android.accessibilityservice.AccessibilityService;
import android.accessibilityservice.InputMethod;
import android.view.inputmethod.EditorInfo;
import org.json.JSONObject;

/** What needs API 33, apart, so that older devices never load it. */
final class Api33 {
    private Api33() {}

    /** Commits text at the cursor of the field that started input with the service's input method. */
    static JSONObject commitText(AccessibilityService service, String text) throws Failure {
        InputMethod inputMethod = service.getInputMethod();
        if (inputMethod == null) {
            throw new Failure("no_input_method", "the service has no input method; its configuration lacks flagInputMethodEditor");
        }
        InputMethod.AccessibilityInputConnection connection = inputMethod.getCurrentInputConnection();
        if (connection == null) {
            throw new Failure("no_focused_field", "no text field has started input with the service's input method");
        }
        connection.commitText(text, 1, null);
        EditorInfo editor = inputMethod.getCurrentInputEditorInfo();
        return Json.obj("package_name", editor == null ? null : editor.packageName);
    }
}
