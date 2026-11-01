package org.spectragent.device;

import java.util.ArrayList;
import java.util.List;
import org.json.JSONArray;
import org.json.JSONObject;

/**
 * A gesture request's strokes, for the service and the helper alike:
 * {@code {"strokes": [{"points": [[x, y, t], ...]}, ...]}}. Each stroke is one finger; t is in ms
 * from the gesture's start and never goes back; the finger goes down at the first point and lifts
 * at the last, so a stroke has two points at least. A tap is two points at one place.
 */
final class Strokes {
    private Strokes() {}

    /** Each stroke's points as {x, y, t}. */
    static List<float[][]> parse(JSONObject params, int maxStrokes) throws Failure {
        JSONArray strokes = params.optJSONArray("strokes");
        if (strokes == null || strokes.length() < 1 || strokes.length() > maxStrokes) {
            throw new Failure("bad_params", "a gesture has 1 to " + maxStrokes + " strokes");
        }
        List<float[][]> parsed = new ArrayList<>();
        for (int s = 0; s < strokes.length(); s++) {
            JSONObject stroke = strokes.optJSONObject(s);
            JSONArray points = stroke == null ? null : stroke.optJSONArray("points");
            if (points == null || points.length() < 2) {
                throw new Failure("bad_params", "stroke " + s + " needs 2 or more points: where and when the finger "
                        + "goes down, and where and when it lifts");
            }
            float[][] path = new float[points.length()][];
            for (int p = 0; p < points.length(); p++) {
                JSONArray point = points.optJSONArray(p);
                double x = point == null ? -1 : point.optDouble(0, -1);
                double y = point == null ? -1 : point.optDouble(1, -1);
                double t = point == null ? -1 : point.optDouble(2, -1);
                if (!(x >= 0 && y >= 0 && t >= 0) || (p > 0 && t < path[p - 1][2])) {
                    throw new Failure("bad_params", "stroke " + s + " point " + p + " is not [x, y, t] with x and y on "
                            + "the screen and t not before the point before it");
                }
                path[p] = new float[] {(float) x, (float) y, (float) t};
            }
            parsed.add(path);
        }
        return parsed;
    }
}
