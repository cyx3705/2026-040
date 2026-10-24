#define ECHO_INPUT_TESTS
#include "input_bridge.cpp"
#include <iostream>
#include <stdexcept>
void check(bool ok) { if (!ok) throw std::runtime_error("modifier policy failed"); }
int main() {
    InputKeys keys;
    check(keys.control(1, true, false) == InputKeys::Pass);
    check(keys.control(1, false, true) == InputKeys::Pass);
    check(keys.control(1, true, true) == InputKeys::EnterChinese);
    check(keys.control(1, true, true) == InputKeys::Consume); // autorepeat
    check(keys.control(2, true, true) == InputKeys::Consume);
    check(keys.control(1, false, true) == InputKeys::Consume); // right Ctrl held
    check(keys.control(2, false, true) == InputKeys::LeaveChinese);
    check(keys.held == 0);
    check(keys.control(2, true, true) == InputKeys::EnterChinese);
    keys.reset();
    check(keys.control(2, false, false) == InputKeys::Pass); // focus/lease lost
    check(keys.control(1, true, true) == InputKeys::EnterChinese);
    check(keys.control(1, false, false) == InputKeys::Pass);
    check(keys.held == 0);
    std::cout << "PASS Ctrl sides, hold/release, repeat, focus loss and foreign keyup\n";
    InputBridge bridge;
    bridge.test_hidden_control();
    std::cout << "PASS hidden inline child EDIT geometry, real IMM open/readback, committed text and preedit cancellation\n";
}
