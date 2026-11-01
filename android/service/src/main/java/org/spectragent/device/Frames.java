package org.spectragent.device;

import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.EOFException;
import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * The channel's frames: {@code u32 length | u32 header length | header | payload}, big-endian,
 * the length counting everything after itself; the header is one JSON object in UTF-8.
 * spectragent/core/capabilities/android/device/channel.py describes the whole protocol.
 */
final class Frames {
    static final int MAX_FRAME_BYTES = 64 * 1024 * 1024;
    static final byte[] EMPTY = new byte[0];

    private Frames() {}

    static final class Frame {
        final JSONObject header;
        final byte[] payload;

        Frame(JSONObject header, byte[] payload) {
            this.header = header;
            this.payload = payload;
        }
    }

    /** The next frame, or null when the stream ended between two frames. */
    static Frame read(DataInputStream in) throws IOException {
        return read(in, MAX_FRAME_BYTES);
    }

    /** The next frame of at most maxBytes, or null when the stream ended between two frames. */
    static Frame read(DataInputStream in, int maxBytes) throws IOException {
        int length;
        try {
            length = in.readInt();
        } catch (EOFException end) {
            return null;
        }
        if (length < 4 || length > maxBytes) {
            throw new IOException("a frame of " + length + " bytes");
        }
        int headerLength = in.readInt();
        if (headerLength < 0 || headerLength > length - 4) {
            throw new IOException("a header of " + headerLength + " bytes in a frame of " + length);
        }
        byte[] header = new byte[headerLength];
        in.readFully(header);
        byte[] payload = new byte[length - 4 - headerLength];
        in.readFully(payload);
        try {
            return new Frame(new JSONObject(new String(header, StandardCharsets.UTF_8)), payload);
        } catch (JSONException notAnObject) {
            throw new IOException("a frame header that is not a JSON object: " + notAnObject.getMessage());
        }
    }

    static byte[] encode(byte[] header, byte[] payload) {
        long length = 4L + header.length + payload.length;
        if (length > MAX_FRAME_BYTES) {
            throw new IllegalArgumentException("a frame of " + length + " bytes");
        }
        return ByteBuffer.allocate(8 + header.length + payload.length)
                .putInt((int) length)
                .putInt(header.length)
                .put(header)
                .put(payload)
                .array();
    }

    static byte[] encode(JSONObject header) {
        return encode(utf8(header.toString()), EMPTY);
    }

    /** A response frame: {@code {"kind": "response", "id": id, "result": <result>}} and the payload. */
    static byte[] response(long id, Reply reply) {
        ByteArrayOutputStream header = new ByteArrayOutputStream(reply.result.length + 48);
        byte[] prefix = utf8("{\"kind\":\"response\",\"id\":" + id + ",\"result\":");
        header.write(prefix, 0, prefix.length);
        header.write(reply.result, 0, reply.result.length);
        header.write('}');
        return encode(header.toByteArray(), reply.payload);
    }

    static byte[] error(long id, String code, String message) {
        return encode(Json.obj("kind", "response", "id", id, "error", Json.obj("code", code, "message", message)));
    }

    static byte[] utf8(String text) {
        return text.getBytes(StandardCharsets.UTF_8);
    }
}
