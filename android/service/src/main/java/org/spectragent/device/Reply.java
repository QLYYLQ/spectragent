package org.spectragent.device;

import org.json.JSONObject;

/** A method's answer: its result, one JSON object in UTF-8, and a payload (a screenshot's PNG). */
final class Reply {
    final byte[] result;
    final byte[] payload;

    Reply(byte[] result, byte[] payload) {
        this.result = result;
        this.payload = payload;
    }

    static Reply of(JSONObject result) {
        return new Reply(Frames.utf8(result.toString()), Frames.EMPTY);
    }

    static Reply of(JSONObject result, byte[] payload) {
        return new Reply(Frames.utf8(result.toString()), payload);
    }
}
