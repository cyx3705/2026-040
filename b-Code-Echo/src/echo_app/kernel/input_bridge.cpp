// INPUT-001/004: a leased native EDIT owns Chinese IMM composition. Browser
// TSF contexts cannot reliably be changed with cross-process IMM messages.
#define NOMINMAX
#include <windows.h>
#include <imm.h>
#include <algorithm>
#include <atomic>
#include <cmath>
#include <functional>
#include <future>
#include <memory>
#include <stdexcept>
#include <string>
#include <thread>
#include "input_keys.h"

namespace {
constexpr UINT work_message = WM_APP + 84;
constexpr unsigned Active = 1, Ready = 2, Chinese = 4, Failed = 8, Verified = 16, Proxy = 32;
thread_local std::string error;
class InputBridge;
InputBridge* instance = nullptr;

class InputBridge {
    std::thread thread;
    DWORD thread_id = 0, target_thread = 0;
    HHOOK hook = nullptr;
    HWND target = nullptr, focus = nullptr, panel = nullptr, edit = nullptr;
    WNDPROC edit_proc = nullptr;
    HIMC context = nullptr;
    HFONT font = nullptr;
    HBRUSH background_brush = nullptr;
    COLORREF foreground_color = RGB(0, 0, 0), background_color = RGB(255, 255, 255);
    double left = 0, top = 0, width = 0, height = 0, viewport_width = 0, viewport_height = 0, font_size = 17;
    int font_pixels = 0;
    bool positioned = false;
    HKL chinese = nullptr, original = nullptr;
    bool composing = false, closing = false;
    std::wstring confirmed, composition_base, committed;
    DWORD composition_start = 0, composition_end = 0;
    uint64_t serial = 0;
    uint64_t retired_owner = 0, retired_at = 0;
    std::wstring retired_committed;
    InputKeys keys;
    std::atomic<uint64_t> owner{0}, refreshed{0};
    std::atomic<unsigned> state{0};

    static HWND focused(HWND root) {
        GUITHREADINFO info{sizeof(info)};
        if (!GetGUIThreadInfo(GetWindowThreadProcessId(root, nullptr), &info)) return nullptr;
        return info.hwndFocus;
    }
    static std::wstring text(HWND window) {
        int size = GetWindowTextLengthW(window);
        std::wstring result(static_cast<size_t>(size) + 1, L'\0');
        result.resize(GetWindowTextW(window, result.data(), size + 1));
        return result;
    }
    bool eligible() {
        if (!(state.load() & Active) || GetTickCount64() - refreshed.load() >= 1200) return false;
        HWND foreground = GetForegroundWindow();
        if ((state.load() & Chinese) && foreground == target) return GetFocus() == edit || focused(target) == edit;
        return foreground == target && focused(target) == focus;
    }
    static bool message(HWND window, UINT msg, WPARAM w, LPARAM l) {
        DWORD_PTR result = 0;
        return window && SendMessageTimeoutW(window, msg, w, l, SMTO_ABORTIFHUNG | SMTO_BLOCK, 100, &result);
    }
    void marker(bool entering) {
        auto vk = entering ? VK_F24 : VK_F23;
        message(focus, WM_KEYDOWN, vk, 1);
        message(focus, WM_KEYUP, vk, 0xc0000001);
    }
    static LRESULT CALLBACK panel_window(HWND window, UINT msg, WPARAM w, LPARAM l) {
        if (msg == WM_NCCREATE) {
            auto create = reinterpret_cast<CREATESTRUCTW*>(l);
            SetWindowLongPtrW(window, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(create->lpCreateParams));
        }
        auto self = reinterpret_cast<InputBridge*>(GetWindowLongPtrW(window, GWLP_USERDATA));
        if (self && msg == WM_CTLCOLOREDIT) {
            auto dc = reinterpret_cast<HDC>(w);
            SetTextColor(dc, self->foreground_color); SetBkColor(dc, self->background_color);
            return reinterpret_cast<LRESULT>(self->background_brush);
        }
        if (self && msg == WM_COMMAND && HIWORD(w) == EN_CHANGE && !self->composing && !self->closing)
            self->confirmed = text(self->edit);
        if (msg == WM_CLOSE) return 0;
        return DefWindowProcW(window, msg, w, l);
    }
    static LRESULT CALLBACK edit_window(HWND window, UINT msg, WPARAM w, LPARAM l) {
        auto self = reinterpret_cast<InputBridge*>(GetWindowLongPtrW(window, GWLP_USERDATA));
        if (!self) return DefWindowProcW(window, msg, w, l);
        if (msg == WM_IME_STARTCOMPOSITION && !self->closing) {
            self->composing = true;
            self->composition_base = self->confirmed;
            SendMessageW(window, EM_GETSEL, reinterpret_cast<WPARAM>(&self->composition_start),
                         reinterpret_cast<LPARAM>(&self->composition_end));
        }
        if (msg == WM_IME_COMPOSITION && (l & GCS_RESULTSTR) && !self->closing) {
            LONG bytes = ImmGetCompositionStringW(self->context, GCS_RESULTSTR, nullptr, 0);
            if (bytes >= 0) {
                std::wstring result(static_cast<size_t>(bytes) / sizeof(wchar_t), L'\0');
                ImmGetCompositionStringW(self->context, GCS_RESULTSTR, result.data(), bytes);
                auto start = std::min<size_t>(self->composition_start, self->composition_base.size());
                auto end = std::min<size_t>(self->composition_end, self->composition_base.size());
                self->confirmed = self->composition_base.substr(0, start) + result + self->composition_base.substr(end);
                self->composition_base = self->confirmed;
                self->composition_start = self->composition_end = static_cast<DWORD>(start + result.size());
            }
        }
        auto result = CallWindowProcW(self->edit_proc, window, msg, w, l);
        if (msg == WM_IME_ENDCOMPOSITION) self->composing = false;
        return result;
    }
    void clean_panel() {
        if (edit) ImmAssociateContext(edit, nullptr);
        if (context) ImmDestroyContext(context);
        if (panel) DestroyWindow(panel);
        if (font) DeleteObject(font);
        if (background_brush) DeleteObject(background_brush);
        background_brush = nullptr;
        edit = panel = nullptr; context = nullptr; font = nullptr;
    }
    void make_panel() {
        WNDCLASSW klass{};
        klass.lpfnWndProc = panel_window;
        klass.hInstance = GetModuleHandleW(nullptr);
        klass.hCursor = LoadCursorW(nullptr, MAKEINTRESOURCEW(32513));
        klass.hbrBackground = reinterpret_cast<HBRUSH>(COLOR_WINDOW + 1);
        klass.lpszClassName = L"EchoChineseInput";
        RegisterClassW(&klass);
        // Hidden storage parent only. During composition this becomes a child
        // of the browser viewport, never a visible top-level popup.
        panel = CreateWindowExW(WS_EX_TOOLWINDOW, klass.lpszClassName, L"",
            WS_POPUP, 0, 0, 1, 1, nullptr, nullptr, klass.hInstance, this);
        if (!panel) throw std::runtime_error("input_panel_unavailable");
        background_brush = CreateSolidBrush(background_color);
        edit = CreateWindowExW(0, L"EDIT", L"", WS_CHILD | WS_VISIBLE | ES_AUTOHSCROLL,
            0, 0, 1, 1, panel, nullptr, klass.hInstance, nullptr);
        if (!edit) throw std::runtime_error("input_edit_unavailable");
        SendMessageW(edit, EM_SETMARGINS, EC_LEFTMARGIN | EC_RIGHTMARGIN, 0);
        SendMessageW(edit, EM_SETLIMITTEXT, 4096, 0);
        SetWindowLongPtrW(edit, GWLP_USERDATA, reinterpret_cast<LONG_PTR>(this));
        edit_proc = reinterpret_cast<WNDPROC>(SetWindowLongPtrW(edit, GWLP_WNDPROC, reinterpret_cast<LONG_PTR>(edit_window)));
        context = ImmCreateContext();
        if (!context) throw std::runtime_error("input_ime_context_unavailable");
        ImmAssociateContext(edit, context);
    }

    bool place_editor() {
        if (!positioned || !IsWindow(target)) return false;
        // Prefer Chromium's actual content HWND, avoiding assumptions about
        // toolbar height, browser zoom, sidebars or the monitor's DPI.
        struct Search { HWND window = nullptr; double ratio = 0; double error = 8; } search;
        search.ratio = viewport_height / viewport_width;
        EnumChildWindows(target, [](HWND child, LPARAM data) -> BOOL {
            auto& found = *reinterpret_cast<Search*>(data);
            wchar_t klass[128]{}; GetClassNameW(child, klass, 128);
            if (std::wstring(klass) != L"Chrome_RenderWidgetHostHWND" || !IsWindowVisible(child)) return TRUE;
            RECT rect{}; GetClientRect(child, &rect);
            double difference = std::abs(rect.bottom - rect.right * found.ratio);
            if (rect.right > 0 && difference < found.error) { found.window = child; found.error = difference; }
            return TRUE;
        }, reinterpret_cast<LPARAM>(&search));
        HWND viewport = search.window ? search.window : target;
        RECT client{}; GetClientRect(viewport, &client);
        double scale = client.right / viewport_width;
        if (scale <= 0 || scale > 8) return false;
        int origin_y = search.window ? 0 : client.bottom - static_cast<int>(std::lround(viewport_height * scale));
        if (origin_y < -2) return false;
        POINT position{static_cast<LONG>(std::lround(left * scale)),
                       origin_y + static_cast<LONG>(std::lround(top * scale))};
        MapWindowPoints(viewport, target, &position, 1);
        int w = std::max(1, static_cast<int>(std::lround(width * scale)));
        int h = std::max(1, static_cast<int>(std::lround(height * scale)));
        // Never show a freestanding window if embedding fails.
        SetWindowLongPtrW(panel, GWL_STYLE, WS_CHILD | WS_CLIPCHILDREN);
        SetParent(panel, target);
        if (GetParent(panel) != target) return false;
        SetWindowPos(panel, nullptr, position.x, position.y, w, h, SWP_NOACTIVATE | SWP_NOZORDER | SWP_FRAMECHANGED);
        SetWindowPos(edit, nullptr, 0, 0, w, h, SWP_NOACTIVATE | SWP_NOZORDER);
        int pixels = std::max(8, static_cast<int>(std::lround(font_size * scale)));
        if (font_pixels != pixels) {
            auto next_font = CreateFontW(-pixels, 0, 0, 0, FW_NORMAL, FALSE, FALSE, FALSE, DEFAULT_CHARSET,
                OUT_DEFAULT_PRECIS, CLIP_DEFAULT_PRECIS, CLEARTYPE_QUALITY, DEFAULT_PITCH, L"Segoe UI");
            if (!next_font) return false;
            SendMessageW(edit, WM_SETFONT, reinterpret_cast<WPARAM>(next_font), TRUE);
            if (font) DeleteObject(font);
            font = next_font; font_pixels = pixels;
        }
        InvalidateRect(edit, nullptr, TRUE);
        return true;
    }
    bool focus_window(HWND foreground, HWND child, DWORD other_thread) {
        bool attached = other_thread != thread_id && AttachThreadInput(thread_id, other_thread, TRUE);
        SetForegroundWindow(foreground);
        SetActiveWindow(foreground);
        SetFocus(child);
        bool ok = GetForegroundWindow() == foreground && GetFocus() == child;
        if (attached) AttachThreadInput(thread_id, other_thread, FALSE);
        return ok;
    }
    void finish(bool return_focus) {
        closing = true;
        // A preedit is never a committed word, even when the IME commits on blur.
        if (context) {
            ImmNotifyIME(context, NI_COMPOSITIONSTR, CPS_CANCEL, 0);
            ImmSetOpenStatus(context, FALSE);
        }
        if (committed.size() + confirmed.size() <= 65536) committed += confirmed;
        else state = Active | Failed | Proxy;
        confirmed.clear(); composition_base.clear(); composing = false;
        SetWindowTextW(edit, L"");
        bool ours = GetForegroundWindow() == target && (GetFocus() == edit || focused(target) == edit);
        ShowWindow(panel, SW_HIDE);
        if (return_focus && ours && IsWindow(target) && IsWindow(focus)) focus_window(target, focus, target_thread);
        SetParent(panel, nullptr);
        SetWindowLongPtrW(panel, GWL_STYLE, WS_POPUP);
        if (original) ActivateKeyboardLayout(original, 0);
        closing = false;
    }
    void mode(bool entering) {
        if (!entering) {
            finish(true);
            if (!(state.load() & Failed)) state = Active | Ready | Proxy;
            marker(false);
            return;
        }
        confirmed.clear(); composing = false;
        closing = true; SetWindowTextW(edit, L""); closing = false;
        original = GetKeyboardLayout(thread_id);
        if (!place_editor()) { state = Active | Failed | Proxy; marker(false); return; }
        state = Active | Ready | Chinese | Proxy;
        marker(true); // Before the DOM blur, so JS keeps this owned lease alive.
        ShowWindow(panel, SW_SHOWNOACTIVATE);
        bool ok = focus_window(target, edit, target_thread);
        if (ok) {
            ActivateKeyboardLayout(chinese, 0);
            ImmAssociateContext(edit, context);
            ImmSetOpenStatus(context, TRUE);
            DWORD conversion = 0, sentence = 0;
            if (ImmGetConversionStatus(context, &conversion, &sentence))
                ImmSetConversionStatus(context, (conversion | IME_CMODE_NATIVE) & ~IME_CMODE_NOCONVERSION, sentence);
            ok = GetKeyboardLayout(thread_id) == chinese && ImmGetOpenStatus(context);
        }
        if (!ok) {
            finish(true); state = Active | Failed | Proxy; marker(false);
        }
    }
    void relinquish() {
        if (!owner.load()) return;
        if ((state.load() & Chinese) || GetParent(panel)) finish(false);
        retired_owner = owner.load(); retired_at = GetTickCount64(); retired_committed = committed;
        state = 0; owner = 0; keys.reset(); committed.clear();
        target = focus = nullptr; positioned = false;
    }
    static LRESULT CALLBACK keyboard(int code, WPARAM msg, LPARAM data) {
        auto self = instance;
        if (code >= 0 && self) {
            auto& key = *reinterpret_cast<KBDLLHOOKSTRUCT*>(data);
            if (key.vkCode == VK_LCONTROL || key.vkCode == VK_RCONTROL || key.vkCode == VK_CONTROL) {
                bool down = msg == WM_KEYDOWN || msg == WM_SYSKEYDOWN;
                unsigned side = key.vkCode == VK_RCONTROL ? 2u : 1u;
                auto action = self->keys.control(side, down, self->eligible() && (self->state.load() & Ready));
                if (action == InputKeys::EnterChinese || action == InputKeys::LeaveChinese) {
                    auto command = new std::function<void()>([self, entering = action == InputKeys::EnterChinese] {
                        if (self->eligible()) self->mode(entering);
                    });
                    if (!PostThreadMessageW(self->thread_id, work_message, 0, reinterpret_cast<LPARAM>(command))) delete command;
                }
                if (action != InputKeys::Pass) return 1;
            }
        }
        return CallNextHookEx(nullptr, code, msg, data);
    }
    template<class F> auto invoke(F function) -> decltype(function()) {
        auto task = std::make_shared<std::packaged_task<decltype(function())()>>(std::move(function));
        auto future = task->get_future();
        auto command = new std::function<void()>([task] { (*task)(); });
        if (!PostThreadMessageW(thread_id, work_message, 0, reinterpret_cast<LPARAM>(command))) {
            delete command; throw std::runtime_error("input_thread_unavailable");
        }
        return future.get();
    }
public:
    InputBridge() {
        if (instance) throw std::runtime_error("input_bridge_already_exists");
        instance = this;
        std::promise<bool> started; auto future = started.get_future();
        thread = std::thread([this, &started] {
            thread_id = GetCurrentThreadId();
            MSG msg; PeekMessageW(&msg, nullptr, 0, 0, PM_NOREMOVE);
            HMODULE module = nullptr;
            GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                              reinterpret_cast<LPCWSTR>(&keyboard), &module);
            try { make_panel(); } catch (...) { clean_panel(); started.set_value(false); return; }
            hook = SetWindowsHookExW(WH_KEYBOARD_LL, keyboard, module, 0);
            SetTimer(nullptr, 1, 100, nullptr);
            started.set_value(hook != nullptr);
            while (GetMessageW(&msg, nullptr, 0, 0) > 0) {
                if (msg.message == work_message) {
                    std::unique_ptr<std::function<void()>> command(reinterpret_cast<std::function<void()>*>(msg.lParam));
                    (*command)();
                } else if (msg.message == WM_TIMER) {
                    if (owner.load() && !eligible()) relinquish();
                    if (retired_owner && GetTickCount64() - retired_at >= 2000) {
                        retired_owner = 0; retired_committed.clear();
                    }
                }
                else { TranslateMessage(&msg); DispatchMessageW(&msg); }
            }
            relinquish(); if (hook) UnhookWindowsHookEx(hook);
            clean_panel();
        });
        if (!future.get()) {
            PostThreadMessageW(thread_id, WM_QUIT, 0, 0); thread.join(); instance = nullptr;
            throw std::runtime_error("input_helper_unavailable");
        }
    }
    ~InputBridge() {
        PostThreadMessageW(thread_id, WM_QUIT, 0, 0); thread.join(); instance = nullptr;
    }
    uint64_t acquire(std::wstring marker_text) {
        return invoke([this, marker_text] {
            HWND window = GetForegroundWindow();
            wchar_t klass[128]{}; GetClassNameW(window, klass, 128);
            std::wstring window_class(klass);
            if (marker_text.size() != 32 || marker_text.find_first_not_of(L"0123456789abcdef") != std::wstring::npos ||
                (window_class.find(L"Chrome_WidgetWin") != 0 && window_class != L"MozillaWindowClass"))
                throw std::runtime_error("input_foreground_mismatch");
            if ((GetAsyncKeyState(VK_CONTROL) | GetAsyncKeyState(VK_MENU) | GetAsyncKeyState(VK_LWIN) |
                 GetAsyncKeyState(VK_RWIN)) & 0x8000) throw std::runtime_error("input_release_modifiers");
            auto edit_focus = focused(window);
            if (!edit_focus) throw std::runtime_error("input_focus_unavailable");
            if (owner.load() && eligible()) throw std::runtime_error("input_busy");
            relinquish();
            target = window; focus = edit_focus; target_thread = GetWindowThreadProcessId(focus, nullptr);
            owner = ++serial; refreshed = GetTickCount64(); state = Active | Proxy;
            if (text(target).find(L"EchoInput-" + marker_text) != std::wstring::npos) state = state.load() | Verified;
            else {
                message(focus, WM_KEYDOWN, VK_F22, 1);
                message(focus, WM_KEYUP, VK_F22, 0xc0000001);
            }
            return owner.load();
        });
    }
    void confirm(uint64_t id) {
        invoke([this, id] {
            if (owner.load() != id || !id || !eligible()) throw std::runtime_error("input_focus_unavailable");
            if (state.load() & Ready) return;
            if (GetAsyncKeyState(VK_CONTROL) & 0x8000) throw std::runtime_error("input_release_modifiers");
            HKL layouts[64]{}; int count = GetKeyboardLayoutList(64, layouts);
            chinese = nullptr;
            for (int i = 0; i < count && !chinese; ++i)
                if (PRIMARYLANGID(LOWORD(reinterpret_cast<ULONG_PTR>(layouts[i]))) == LANG_CHINESE) chinese = layouts[i];
            if (!chinese) { relinquish(); throw std::runtime_error("input_chinese_not_installed"); }
            state = Active | Ready | Proxy;
        });
    }
    void bounds(uint64_t id, double x, double y, double w, double h, double vw, double vh, double size,
                unsigned color, unsigned background) {
        invoke([=] {
            if (owner.load() != id || !id) return; // Preserve the retired final-text response.
            if (!std::isfinite(x + y + w + h + vw + vh + size) || x < 0 || y < 0 || w <= 0 || h <= 0 ||
                vw <= 0 || vh <= 0 || vw > 32768 || vh > 32768 || x + w > vw + 2 || y + h > vh + 2 ||
                size < 8 || size > 128) throw std::runtime_error("input_bounds_invalid");
            left = x; top = y; width = w; height = h; viewport_width = vw; viewport_height = vh; font_size = size;
            foreground_color = color;
            if (background_color != background) {
                background_color = background;
                if (background_brush) DeleteObject(background_brush);
                background_brush = CreateSolidBrush(background_color);
            }
            positioned = true;
            // While Ctrl is held, preserve the original anchor instead of
            // following the browser's hidden caret or an old heartbeat.
        });
    }
    unsigned refresh(uint64_t id) {
        if (owner.load() != id || !id) return 0;
        refreshed = GetTickCount64(); return state.load();
    }
    std::wstring committed_text(uint64_t id) {
        return invoke([this, id] {
            if (owner.load() == id && id) return committed;
            if (retired_owner == id && id && GetTickCount64() - retired_at < 2000) return retired_committed;
            retired_committed.clear(); return std::wstring();
        });
    }
    void release(uint64_t id) {
        invoke([this, id] { if (owner.load() == id && id) relinquish(); });
    }
#ifdef ECHO_INPUT_TESTS
    void test_hidden_control() {
        invoke([this] {
            if (!IsWindow(edit) || !context || IsWindowVisible(panel)) throw std::runtime_error("hidden_edit_lifecycle");
            const auto foreground = GetForegroundWindow();
            HWND host = CreateWindowExW(0, L"STATIC", L"", WS_OVERLAPPEDWINDOW,
                0, 0, 1000, 700, nullptr, nullptr, GetModuleHandleW(nullptr), nullptr);
            if (!host) throw std::runtime_error("test_host_unavailable");
            RECT client{}; GetClientRect(host, &client);
            target = focus = host;
            left = 20; top = 30; width = 200; height = 26;
            viewport_width = client.right; viewport_height = client.bottom; positioned = true;
            if (!place_editor() || GetParent(panel) != host || !(GetWindowLongPtrW(panel, GWL_STYLE) & WS_CHILD) ||
                (GetWindowLongPtrW(panel, GWL_STYLE) & WS_POPUP) || IsWindowVisible(panel))
                throw std::runtime_error("native_inline_child_geometry");
            RECT editor{}; GetWindowRect(panel, &editor);
            MapWindowPoints(nullptr, host, reinterpret_cast<POINT*>(&editor), 2);
            if (editor.left != 20 || editor.top != 30 || editor.right - editor.left != 200 || editor.bottom - editor.top != 26)
                throw std::runtime_error("native_inline_position");
            finish(false); DestroyWindow(host); target = focus = nullptr; positioned = false;
            const auto own_layout = GetKeyboardLayout(thread_id);
            HKL layouts[64]{}; int count = GetKeyboardLayoutList(64, layouts);
            HKL test_chinese = nullptr;
            for (int i = 0; i < count && !test_chinese; ++i)
                if (PRIMARYLANGID(LOWORD(reinterpret_cast<ULONG_PTR>(layouts[i]))) == LANG_CHINESE) test_chinese = layouts[i];
            if (!test_chinese) throw std::runtime_error("test_requires_installed_chinese");
            ActivateKeyboardLayout(test_chinese, 0);
            ImmSetOpenStatus(context, TRUE);
            bool open = ImmGetOpenStatus(context) != FALSE;
            ImmSetOpenStatus(context, FALSE);
            ActivateKeyboardLayout(own_layout, 0);
            if (!open) throw std::runtime_error("native_ime_open_readback");
            SetWindowTextW(edit, L"已确认");
            if (confirmed != L"已确认") throw std::runtime_error("native_edit_committed_tracking");
            composing = true;
            SetWindowTextW(edit, L"已确认pin yin");
            finish(false);
            if (committed != L"已确认" || !text(edit).empty()) throw std::runtime_error("native_preedit_cancel");
            if (GetForegroundWindow() != foreground) throw std::runtime_error("test_changed_foreground");
            committed.clear();
        });
    }
#endif
};
template<class F> int protect(F fn) {
    try { fn(); error.clear(); return 0; }
    catch (const std::exception& ex) { error = ex.what(); return -1; }
    catch (...) { error = "input_unknown_error"; return -1; }
}
}
#define EXPORT extern "C" __declspec(dllexport)
EXPORT const char* echo_input_error() { return error.c_str(); }
EXPORT int echo_input_create(void** out) { return protect([&] { *out = new InputBridge; }); }
EXPORT void echo_input_destroy(void* value) { delete static_cast<InputBridge*>(value); }
EXPORT int echo_input_acquire(void* value, const wchar_t* marker, uint64_t* owner) {
    return protect([&] { *owner = static_cast<InputBridge*>(value)->acquire(marker); });
}
EXPORT int echo_input_confirm(void* value, uint64_t owner) {
    return protect([&] { static_cast<InputBridge*>(value)->confirm(owner); });
}
EXPORT int echo_input_bounds(void* value, uint64_t owner, double x, double y, double w, double h,
                             double vw, double vh, double size, unsigned color, unsigned background) {
    return protect([&] { static_cast<InputBridge*>(value)->bounds(owner, x, y, w, h, vw, vh, size, color, background); });
}
EXPORT unsigned echo_input_refresh(void* value, uint64_t owner) { return static_cast<InputBridge*>(value)->refresh(owner); }
EXPORT int echo_input_text(void* value, uint64_t owner, wchar_t* out, unsigned capacity) {
    return protect([&] {
        auto result = static_cast<InputBridge*>(value)->committed_text(owner);
        if (result.size() >= capacity) throw std::runtime_error("input_text_capacity");
        std::copy(result.begin(), result.end(), out); out[result.size()] = L'\0';
    });
}
EXPORT int echo_input_release(void* value, uint64_t owner) {
    return protect([&] { static_cast<InputBridge*>(value)->release(owner); });
}
