package org.spectragent.device;

import org.json.JSONException;
import org.json.JSONObject;

/** JSON objects built in one expression; org.json declares a checked exception it never throws here. */
final class Json {
    private Json() {}

    /** An object of key, value, key, value, ...; a null value is JSON null. */
    static JSONObject obj(Object... pairs) {
        JSONObject object = new JSONObject();
        try {
            for (int i = 0; i < pairs.length; i += 2) {
                object.put((String) pairs[i], pairs[i + 1] == null ? JSONObject.NULL : pairs[i + 1]);
            }
        } catch (JSONException impossible) { // only for a NaN or infinite number
            throw new IllegalArgumentException(impossible);
        }
        return object;
    }
}
