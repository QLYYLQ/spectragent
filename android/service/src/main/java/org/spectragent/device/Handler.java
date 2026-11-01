package org.spectragent.device;

import org.json.JSONObject;

/** A program's methods: the service's or the helper's. */
interface Handler {
    /** Answers one request, on the connection's thread; a Failure answers with its error. */
    Reply handle(String method, JSONObject params) throws Failure;
}
