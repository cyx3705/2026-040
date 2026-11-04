#pragma once
// INPUT-001: only modifier state is retained; no letters/text are collected.
struct InputKeys {
    unsigned held = 0;
    enum Action { Pass, EnterChinese, LeaveChinese, Consume };
    Action control(unsigned side, bool down, bool eligible) {
        if (!eligible) { held = 0; return Pass; }
        if (down) {
            bool first = held == 0;
            held |= side;
            return first ? EnterChinese : Consume;
        }
        if (!(held & side)) return Pass;
        held &= ~side;
        return held ? Consume : LeaveChinese;
    }
    void reset() { held = 0; }
};
