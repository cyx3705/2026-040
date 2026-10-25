// PERF-003: inference only. HTTP, prompts, dictionary and persistence stay in Python.
#include "llama_api.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <iomanip>
#include <memory>
#include <mutex>
#include <numeric>
#include <set>
#include <sstream>
#include <thread>
#include <vector>

namespace echo {
using Clock = std::chrono::steady_clock;
using Tokens = std::vector<int32_t>;
static void require(bool ok, const char* message) { if (!ok) throw std::runtime_error(message); }

static std::string quote(const std::string& value) {
    std::string out = "\"";
    for (unsigned char c : value) {
        if (c == '"' || c == '\\') { out += '\\'; out += c; }
        else if (c < 32) {
            const char* hex = "0123456789abcdef";
            out += "\\u00"; out += hex[c >> 4]; out += hex[c & 15];
        } else out += c;
    }
    return out + '"';
}

// Stable ties match heapq.nlargest on ascending token IDs. No Python vocabulary scan.
static Tokens top_ids(const float* logits, int n, int limit) {
    require(logits && n > 0 && limit > 0 && limit <= n, "invalid_top_k");
    Tokens ids(n);
    std::iota(ids.begin(), ids.end(), 0);
    for (int i = 0; i < n; ++i) require(std::isfinite(logits[i]), "nonfinite_logits");
    auto better = [&](int a, int b) { return logits[a] > logits[b] || (logits[a] == logits[b] && a < b); };
    std::partial_sort(ids.begin(), ids.begin() + limit, ids.end(), better);
    ids.resize(limit);
    return ids;
}

struct Boundary { size_t bytes; int tokens; };
struct Sequence {
    uint64_t id;
    Tokens tokens;
    int prefix;
    std::string generated, pending;
    std::vector<Boundary> boundaries;
    void* sampler = nullptr;
    int next = -1;
    Clock::time_point touched = Clock::now();
    bool session = false;
};
// KV-001: a reader session's seq0 cache. Python names the file; switching to
// another session, chat, prediction or shutdown writes it there first.
struct Resident { std::string path; Tokens tokens; bool dirty = false; };
struct Branch {
    uint64_t id, source;
    int prefix, steps = 0;
    Clock::time_point created = Clock::now();
    bool decoding = false;
};
struct PredictionScore { std::string token; float logit; };

// Return complete UTF-8 only, retaining split codepoints between model tokens.
static std::string complete_utf8(std::string& pending) {
    size_t i = 0;
    while (i < pending.size()) {
        unsigned char c = pending[i];
        size_t n = c < 0x80 ? 1 : c >= 0xc2 && c <= 0xdf ? 2 :
                   c >= 0xe0 && c <= 0xef ? 3 : c >= 0xf0 && c <= 0xf4 ? 4 : 0;
        require(n != 0, "invalid_token_utf8");
        if (i + n > pending.size()) break;
        for (size_t j = 1; j < n; ++j)
            require((static_cast<unsigned char>(pending[i+j]) & 0xc0) == 0x80, "invalid_token_utf8");
        if (n >= 3) {
            unsigned char b = pending[i+1];
            require(!(c == 0xe0 && b < 0xa0) && !(c == 0xed && b >= 0xa0) &&
                    !(c == 0xf0 && b < 0x90) && !(c == 0xf4 && b >= 0x90), "invalid_token_utf8");
        }
        i += n;
    }
    auto out = pending.substr(0, i);
    pending.erase(0, i);
    return out;
}

class Runtime {
public:
    Api api;
    std::recursive_mutex mutex;
    int context;
    void *model = nullptr, *ctx = nullptr, *vocab = nullptr, *memory = nullptr, *batch = nullptr;
    int n_vocab = 0;
    std::unique_ptr<Sequence> a;
    std::shared_ptr<Branch> b;
    uint64_t serial = 0, forks = 0, releases = 0;
    // PERF-004: one idle cache in seq0, never a third sequence or another model.
    Tokens prediction_tokens;
    std::vector<PredictionScore> prediction_scores;
    Clock::time_point prediction_touched = Clock::now();
    int prediction_reused = 0, prediction_evaluated = 0;
    std::unique_ptr<Resident> resident;
    uint64_t session_saves = 0, session_loads = 0, session_save_failures = 0;
    int session_reused = 0, session_evaluated = 0, session_loaded = 0;

    Runtime(Api functions, const std::string& path, int window, int gpu) : api(functions), context(window) {
        require(window >= 1024, "invalid_context_size");
        api.backend_init();
        try {
            auto mp = api.model_default_params(); mp.n_gpu_layers = gpu;
            model = api.model_load_from_file(path.c_str(), mp);
            require(model != nullptr, "model_load_failed");
            auto cp = api.context_default_params();
            cp.n_ctx = window; cp.n_batch = cp.n_ubatch = std::min(window, 512);
            cp.n_seq_max = 3; cp.n_outputs_max = cp.n_outputs_max_per_seq = 0;
            cp.n_threads = cp.n_threads_batch = std::max(1u, std::min(16u, std::thread::hardware_concurrency()));
            cp.kv_unified = true;
            ctx = api.init_from_model(model, cp); require(ctx != nullptr, "context_init_failed");
            vocab = api.model_get_vocab(model); memory = api.get_memory(ctx);
            batch = api.batch_ext_init(ctx); require(batch != nullptr, "batch_init_failed");
            n_vocab = api.vocab_n_tokens(vocab); require(n_vocab > 0, "invalid_vocabulary");
        } catch (...) { cleanup(); throw; }
    }
    ~Runtime() { cleanup(); }
    void cleanup() {
        // Shutdown keeps the reader context for the next start; never throw here.
        if (ctx && resident) { try { evict(); } catch (...) {} }
        if (a && a->sampler) api.sampler_free(a->sampler);
        a.reset(); b.reset();
        if (batch) api.batch_ext_free(batch);
        if (ctx) api.free(ctx);
        if (model) api.model_free(model);
        batch = ctx = model = nullptr;
        api.backend_free();
    }
    void release_b(uint64_t id) {
        std::lock_guard lock(mutex);
        if (b && b->id == id) {
            require(api.memory_seq_rm(memory, 1, -1, -1), "branch_release_failed");
            b.reset(); ++releases;
        }
    }
    void release_a(uint64_t id) {
        std::lock_guard lock(mutex);
        if (!a || a->id != id) return;
        if (b) release_b(b->id);
        // A session's prefix stays resident in seq0 for its next request.
        if (!a->session) require(api.memory_seq_rm(memory, 0, -1, -1), "sequence_release_failed");
        if (a->sampler) api.sampler_free(a->sampler);
        a.reset();
    }
    void discard_prediction() {
        // Invalidate scores before removing KV: a failed removal cannot yield a hit.
        prediction_scores.clear();
        require(api.memory_seq_rm(memory, 0, -1, -1), "prediction_release_failed");
        prediction_tokens.clear();
        prediction_reused = prediction_evaluated = 0;
    }
    // Write the resident session when changed. Callers hold the lock and no A.
    void save() {
        if (!resident || !resident->dirty || resident->tokens.empty()) return;
        auto& item = *resident;
        auto target = std::filesystem::u8path(item.path), temporary = std::filesystem::u8path(item.path + ".tmp");
        // Write beside, then replace: a crash never leaves a half file under the real name.
        bool ok = api.state_seq_save_file(ctx, (item.path + ".tmp").c_str(), 0, item.tokens.data(), item.tokens.size()) > 0;
        std::error_code error;
        if (ok) { std::filesystem::rename(temporary, target, error); ok = !error; }
        if (ok) { ++session_saves; item.dirty = false; }
        else { ++session_save_failures; std::filesystem::remove(temporary, error); }
    }
    void evict() {
        if (!resident) return;
        save(); resident.reset();
        require(api.memory_seq_rm(memory, 0, -1, -1), "session_release_failed");
    }
    // Idle write-back, so a killed adapter loses nothing already translated.
    void flush() {
        std::lock_guard lock(mutex);
        if (!a) save();
    }
    std::pair<uint64_t, int> begin_session(const std::string& prompt, const std::string& path) {
        std::lock_guard lock(mutex); expire(); require(!a, "only_one_A_sequence_supported");
        require(!path.empty(), "invalid_session_path");
        auto tokens = tokenize(prompt, true);
        require(!tokens.empty() && tokens.size() <= size_t(context - 768), "context_limit");
        session_loaded = 0;
        if (!resident || resident->path != path) {
            evict(); discard_prediction();
            auto next = std::make_unique<Resident>(); next->path = path;
            std::error_code error;
            if (std::filesystem::exists(std::filesystem::u8path(path), error)) {
                Tokens loaded(context); size_t count = 0;
                // A stale, foreign or truncated file is only a cache miss.
                if (api.state_seq_load_file(ctx, path.c_str(), 0, loaded.data(), loaded.size(), &count) > 0 &&
                    count > 0 && count <= loaded.size() && api.memory_seq_pos_max(memory, 0) == int(count) - 1) {
                    loaded.resize(count); next->tokens = std::move(loaded);
                    ++session_loads; session_loaded = int(count);
                } else require(api.memory_seq_rm(memory, 0, -1, -1), "session_release_failed");
            }
            resident = std::move(next);
        }
        auto& r = *resident;
        size_t common = 0;
        while (common < tokens.size() && common < r.tokens.size() && tokens[common] == r.tokens[common]) ++common;
        try {
            if (common < r.tokens.size()) {
                require(api.memory_seq_rm(memory, 0, int(common), -1), "session_trim_failed");
                r.tokens.resize(common); r.dirty = true;
            }
            if (common < tokens.size()) {
                decode(Tokens(tokens.begin() + common, tokens.end()), 0, int(common));
                r.dirty = true;
            }
        } catch (...) { resident.reset(); api.memory_seq_rm(memory, 0, -1, -1); throw; }
        r.tokens = tokens;
        session_reused = int(common); session_evaluated = int(tokens.size() - common);
        auto next = std::make_unique<Sequence>();
        next->id = ++serial; next->prefix = int(tokens.size()); next->tokens = std::move(tokens); next->session = true;
        auto result = std::make_pair(next->id, next->prefix); a = std::move(next); return result;
    }
    void expire() {
        auto now = Clock::now();
        if (b && now - b->created > std::chrono::seconds(180)) release_b(b->id);
        if (a && now - a->touched > std::chrono::seconds(180)) release_a(a->id);
        if (!prediction_tokens.empty() && now - prediction_touched > std::chrono::seconds(180)) discard_prediction();
    }
    Sequence& sequence(uint64_t id) {
        require(a && a->id == id, "sequence_unavailable");
        a->touched = Clock::now(); return *a;
    }
    Tokens tokenize(const std::string& text, bool special) {
        std::lock_guard lock(mutex);
        Tokens out(std::max<size_t>(32, text.size() * 4 + 16));
        int n = api.tokenize(vocab, text.data(), int(text.size()), out.data(), int(out.size()), special, true);
        if (n < 0) { out.resize(-n); n = api.tokenize(vocab, text.data(), int(text.size()), out.data(), int(out.size()), special, true); }
        require(n >= 0, "tokenize_failed"); out.resize(n);
        for (int t : out) require(t >= 0 && t < n_vocab, "invalid_token_id");
        return out;
    }
    std::string piece(int token) {
        std::string out(256, '\0');
        int n = api.token_to_piece(vocab, token, out.data(), int(out.size()), 0, true);
        if (n < 0) { out.resize(-n); n = api.token_to_piece(vocab, token, out.data(), int(out.size()), 0, true); }
        require(n >= 0, "token_piece_failed"); out.resize(n); return out;
    }
    // Caller owns context lock. All batch assembly and model calls stay in C++.
    void decode(const Tokens& tokens, int seq, int start, bool output = true) {
        require(!tokens.empty() && start >= 0 && size_t(start) + tokens.size() <= size_t(context), "context_limit");
        for (size_t offset = 0; offset < tokens.size(); offset += 512) {
            size_t end = std::min(offset + 512, tokens.size());
            api.batch_ext_clear(batch);
            try {
                for (size_t i = offset; i < end; ++i) {
                    require(tokens[i] >= 0 && tokens[i] < n_vocab, "invalid_token_id");
                    int idx = api.batch_ext_add_token(batch, seq, tokens[i]);
                    require(idx >= 0, "batch_add_failed");
                    int32_t pos = start + int(i);
                    require(api.batch_ext_set_pos(batch, idx, &pos), "batch_position_failed");
                    require(api.batch_ext_set_output_logits(batch, idx, output && i + 1 == tokens.size()), "batch_output_failed");
                }
                require(api.process(ctx, 1, batch) == 0, "llama_process_failed");
            } catch (...) { api.batch_ext_clear(batch); throw; }
            api.batch_ext_clear(batch);
        }
    }
    float* logits() { auto p = api.get_logits_ith(ctx, -1); require(p != nullptr, "missing_final_logits"); return p; }
    void* sampler(uint32_t seed) {
        auto params = api.sampler_chain_default_params(); params.no_perf = true;
        void* chain = api.sampler_chain_init(params); require(chain != nullptr, "sampler_init_failed");
        try {
            auto add = [&](void* stage) { require(stage != nullptr, "sampler_stage_failed"); api.sampler_chain_add(chain, stage); };
            add(api.sampler_init_top_k(64)); add(api.sampler_init_temp(.8f));
            add(api.sampler_init_top_p(.95f, 1)); add(api.sampler_init_dist(seed));
        } catch (...) { api.sampler_free(chain); throw; }
        return chain;
    }
    std::pair<uint64_t, int> begin(const std::string& prompt, uint32_t seed, bool sample) {
        std::lock_guard lock(mutex); expire(); require(!a, "only_one_A_sequence_supported");
        auto tokens = tokenize(prompt, true);
        require(tokens.size() <= size_t(context - 768), "context_limit");
        // Formal A always starts clean, preserving seeded A/B isolation and the
        // entire original KV budget. Prediction cache has no public A handle.
        evict(); discard_prediction();
        auto next = std::make_unique<Sequence>();
        next->id = ++serial; next->prefix = int(tokens.size()); next->tokens = std::move(tokens);
        try {
            if (sample) next->sampler = sampler(seed);
            decode(next->tokens, 0, 0);
            if (sample) next->next = api.sampler_sample(next->sampler, ctx, -1);
        } catch (...) {
            if (next->sampler) api.sampler_free(next->sampler);
            api.memory_seq_rm(memory, 0, -1, -1); throw;
        }
        auto result = std::make_pair(next->id, next->prefix); a = std::move(next); return result;
    }
    // One model step per call allows A/B interleaving; no Python sampling loop.
    std::pair<bool, std::string> step(uint64_t id) {
        std::lock_guard lock(mutex); auto& s = sequence(id);
        require(s.sampler != nullptr, "sequence_not_sampled");
        require(s.next >= 0 && s.next < n_vocab, "invalid_sampled_token");
        if (s.tokens.size() >= size_t(context - 768) || api.vocab_is_eog(vocab, s.next)) return {true, ""};
        int token = s.next;
        s.pending += piece(token);
        auto text = complete_utf8(s.pending);
        decode({token}, 0, int(s.tokens.size()));
        s.next = api.sampler_sample(s.sampler, ctx, -1);
        s.tokens.push_back(token); s.generated += text;
        // Partial UTF-8 tokens cannot be safe shared character boundaries.
        if (s.pending.empty()) s.boundaries.push_back({s.generated.size(), int(s.tokens.size())});
        return {false, text};
    }
    std::pair<int, std::string> prefix(uint64_t id, const std::string& text) {
        std::lock_guard lock(mutex); auto& s = sequence(id);
        require(s.generated.compare(0, text.size(), text) == 0, "prefix_text_mismatch");
        auto it = std::upper_bound(s.boundaries.begin(), s.boundaries.end(), text.size(),
            [](size_t bytes, const Boundary& p) { return bytes < p.bytes; });
        if (it == s.boundaries.begin()) return {s.prefix, text};
        --it; return {it->tokens, text.substr(it->bytes)};
    }
    uint64_t fork(uint64_t id, int prefix) {
        std::lock_guard lock(mutex); expire(); auto& s = sequence(id);
        require(prefix >= 0 && prefix <= int(s.tokens.size()), "invalid_prefix_length");
        require(!b, "branch_busy");
        if (prefix) {
            api.memory_seq_cp(memory, 0, 1, 0, prefix);
            if (api.memory_seq_pos_max(memory, 1) != prefix - 1) {
                api.memory_seq_rm(memory, 1, -1, -1); throw std::runtime_error("prefix_copy_mismatch");
            }
        }
        b = std::make_shared<Branch>(); b->id = ++serial; b->source = id; b->prefix = prefix;
        ++forks; return b->id;
    }
    void check_branch(const std::shared_ptr<Branch>& item) {
        require(b == item, "branch_cancelled");
        require(Clock::now() - item->created <= std::chrono::seconds(25), "deadline");
    }
    // All label tokenization, constrained trie decisions and forward steps live here.
    std::pair<std::string, int> select(uint64_t id, const Tokens& suffix,
                                     const std::vector<std::string>& labels, int budget) {
        std::unique_lock lock(mutex);
        require(b && b->id == id, "branch_unavailable"); auto item = b;
        require(!item->decoding, "branch_busy");
        require(!suffix.empty() && suffix.size() <= 640, "suffix_budget");
        require(budget >= 1 && budget <= 32, "label_budget");
        require(!labels.empty() && labels.size() <= 256 &&
                std::set<std::string>(labels.begin(), labels.end()).size() == labels.size(), "invalid_allowed_labels");
        require(api.memory_seq_pos_max(memory, 1) + 1 == item->prefix, "prefix_position_mismatch");
        item->decoding = true;
        try {
            std::vector<float> snapshot;
            for (size_t offset = 0; offset < suffix.size(); offset += 64) {
                check_branch(item);
                size_t end = std::min(offset + 64, suffix.size()); bool last = end == suffix.size();
                decode(Tokens(suffix.begin() + offset, suffix.begin() + end), 1, item->prefix + int(offset), last);
                if (last) { auto p = logits(); snapshot.assign(p, p + n_vocab); }
                // Always reacquire and validate identity, including after final prefill.
                lock.unlock();
                if (!last) std::this_thread::sleep_for(std::chrono::milliseconds(1));
                else std::this_thread::yield();
                lock.lock(); check_branch(item);
            }
            std::vector<Tokens> paths; bool single = true;
            for (auto& label : labels) { paths.push_back(tokenize(label, false)); single &= paths.back().size() == 1; }
            auto choose = [&](const Tokens& choices) {
                int token = -1;
                for (int t : choices) {
                    require(t >= 0 && t < n_vocab, "invalid_token_id");
                    require(std::isfinite(snapshot[t]), "nonfinite_label_logits");
                    if (token < 0 || snapshot[t] > snapshot[token]) token = t;
                }
                require(token >= 0, "label_budget"); return token;
            };
            auto finish = [&](size_t label) {
                auto result = std::make_pair(labels[label], item->steps);
                // A completed B has no further use. Free its exclusive cells
                // before yielding the context, not after another HTTP trip.
                release_b(item->id);
                return result;
            };
            if (single) {
                Tokens choices; for (auto& path : paths) choices.push_back(path[0]);
                require(std::set<int>(choices.begin(), choices.end()).size() == choices.size(), "ambiguous_label_tokenization");
                int token = choose(choices); item->steps = 1;
                return finish(std::find(choices.begin(), choices.end(), token) - choices.begin());
            }
            paths.clear();
            for (auto& label : labels) { paths.push_back(tokenize(label + "\n", false)); require(!paths.back().empty(), "label_budget"); }
            std::vector<size_t> active(labels.size()); std::iota(active.begin(), active.end(), 0);
            for (int step = 0; step < budget; ++step) {
                check_branch(item);
                Tokens choices;
                for (size_t i : active) { require(paths[i].size() > size_t(step), "ambiguous_label_tokenization"); choices.push_back(paths[i][step]); }
                int token = choose(choices);
                active.erase(std::remove_if(active.begin(), active.end(), [&](size_t i) { return paths[i][step] != token; }), active.end());
                item->steps = step + 1;
                if (active.size() == 1) return finish(active[0]);
                for (size_t i : active) require(paths[i].size() > size_t(step + 1), "ambiguous_label_tokenization");
                if (step + 1 == budget) break;
                decode({token}, 1, item->prefix + int(suffix.size()) + step);
                auto p = logits(); snapshot.assign(p, p + n_vocab);
            }
            throw std::runtime_error("label_budget");
        } catch (...) {
            // A cancelled decoder must never reset a replacement branch's state.
            if (!lock.owns_lock()) lock.lock();
            if (b == item) item->decoding = false;
            throw;
        }
    }
    // WEB-002: two input-KV branches share seq0. Suffixes are evaluated in the
    // SAME llama batch, not two independent model copies or HTTP decoders.
    // This bounded operation owns the lock; chat's sampled A is never eligible.
    std::string select_batch(uint64_t source, const std::vector<Tokens>& suffixes,
                             const std::vector<std::vector<std::string>>& labels) {
        std::lock_guard lock(mutex); auto& s = sequence(source);
        require(!s.sampler && s.generated.empty() && !b, "input_sequence_required");
        size_t count = suffixes.size(), total = 0;
        require(count >= 1 && count <= 2 && labels.size() == count, "invalid_batch_size");
        std::vector<Tokens> choices(count);
        for (size_t j = 0; j < count; ++j) {
            require(!suffixes[j].empty() && suffixes[j].size() <= 640, "suffix_budget");
            total += suffixes[j].size();
            require(!labels[j].empty() && labels[j].size() <= 64, "invalid_allowed_labels");
            for (auto& label : labels[j]) {
                auto path = tokenize(label, false);
                require(path.size() == 1, "single_token_labels_required");
                choices[j].push_back(path[0]);
            }
            require(std::set<int>(choices[j].begin(), choices[j].end()).size() == choices[j].size(),
                    "ambiguous_label_tokenization");
        }
        require(total <= 704 && s.tokens.size() + total <= size_t(context), "batch_context_limit");
        std::vector<int> allocated;
        auto clear = [&] {
            bool ok = true;
            for (int seq : allocated) {
                if (api.memory_seq_rm(memory, seq, -1, -1)) ++releases;
                else ok = false;
            }
            allocated.clear(); require(ok, "branch_release_failed");
        };
        try {
            for (size_t j = 0; j < count; ++j) {
                int seq = int(j) + 1;
                allocated.push_back(seq); ++forks;
                api.memory_seq_cp(memory, 0, seq, 0, int(s.tokens.size()));
                require(api.memory_seq_pos_max(memory, seq) == int(s.tokens.size()) - 1, "prefix_copy_mismatch");
            }
            std::vector<std::string> selected(count);
            for (size_t offset = 0; ; offset += 64) {
                api.batch_ext_clear(batch); std::vector<size_t> outputs; bool any = false;
                for (size_t j = 0; j < count; ++j) {
                    size_t end = std::min(offset + 64, suffixes[j].size());
                    for (size_t i = offset; i < end; ++i) {
                        any = true; int token = suffixes[j][i];
                        require(token >= 0 && token < n_vocab, "invalid_token_id");
                        int idx = api.batch_ext_add_token(batch, int(j) + 1, token);
                        require(idx >= 0, "batch_add_failed");
                        int32_t pos = int(s.tokens.size() + i);
                        require(api.batch_ext_set_pos(batch, idx, &pos), "batch_position_failed");
                        bool last = i + 1 == suffixes[j].size();
                        require(api.batch_ext_set_output_logits(batch, idx, last), "batch_output_failed");
                        if (last) outputs.push_back(j);
                    }
                }
                if (!any) break;
                require(api.process(ctx, 1, batch) == 0, "llama_process_failed");
                for (size_t k = 0; k < outputs.size(); ++k) {
                    size_t j = outputs[k];
                    auto scores = api.get_logits_ith(ctx, int(k) - int(outputs.size()));
                    require(scores != nullptr, "missing_final_logits");
                    size_t best = 0;
                    for (size_t i = 0; i < choices[j].size(); ++i) {
                        require(std::isfinite(scores[choices[j][i]]), "nonfinite_logits");
                        if (scores[choices[j][i]] > scores[choices[j][best]]) best = i;
                    }
                    selected[j] = labels[j][best];
                }
            }
            api.batch_ext_clear(batch); clear();
            std::string out = "[";
            for (size_t j = 0; j < count; ++j) {
                if (j) out += ',';
                out += "{\"label\":" + quote(selected[j]) + ",\"decision_steps\":1}";
            }
            return out + ']';
        } catch (...) { api.batch_ext_clear(batch); clear(); throw; }
    }
    std::string predict(const std::string& prompt, int limit) {
        std::lock_guard lock(mutex); require(limit >= 1 && limit <= 20, "invalid_top_k");
        expire(); require(!a && !b, "only_one_A_sequence_supported");
        evict();
        auto tokens = tokenize(prompt, true);
        require(!tokens.empty() && tokens.size() <= size_t(context - 768), "context_limit");
        prediction_reused = prediction_evaluated = 0;
        try {
            if (tokens != prediction_tokens || prediction_scores.empty()) {
                size_t common = 0;
                while (common < tokens.size() && common < prediction_tokens.size() &&
                       tokens[common] == prediction_tokens[common]) ++common;
                // Deletion can leave no new tokens: recompute the last position
                // for its logits. Compare actual tokens, never string prefixes.
                common = std::min(common, tokens.size() - 1);
                prediction_scores.clear(); prediction_tokens.clear();
                require(api.memory_seq_rm(memory, 0, int(common), -1), "prediction_trim_failed");
                decode(Tokens(tokens.begin() + common, tokens.end()), 0, int(common));
                auto scores = logits(); auto ids = top_ids(scores, n_vocab, std::min(20, n_vocab));
                for (int id : ids) prediction_scores.push_back({piece(id), scores[id]});
                prediction_tokens = std::move(tokens);
                prediction_reused = int(common);
                prediction_evaluated = int(prediction_tokens.size() - common);
            } else {
                prediction_reused = int(tokens.size());
            }
            prediction_touched = Clock::now();
            size_t count = std::min(size_t(limit), prediction_scores.size());
            double maximum = prediction_scores[0].logit, sum = 0;
            for (size_t i = 0; i < count; ++i) sum += std::exp(double(prediction_scores[i].logit) - maximum);
            double norm = maximum + std::log(sum);
            std::ostringstream out; out << std::setprecision(17) << '[';
            for (size_t i = 0; i < count; ++i) {
                if (i) out << ',';
                out << "{\"token\":" << quote(prediction_scores[i].token) << ",\"logprob\":" << double(prediction_scores[i].logit) - norm << '}';
            }
            out << ']'; return out.str();
        } catch (...) { discard_prediction(); throw; }
    }
    std::string info() {
        std::lock_guard lock(mutex); expire();
        int tokens = a ? int(a->tokens.size()) : 0;
        // An idle resident session still occupies seq0 cells.
        int idle = !a && resident ? int(resident->tokens.size()) : 0;
        int shared = b ? b->prefix : 0;
        int exclusive = b ? std::max(0, api.memory_seq_pos_max(memory, 1) + 1 - b->prefix) : 0;
        return "{\"kernel\":\"cpp\",\"kernel_abi\":2,\"active_sequences\":" + std::to_string(a ? 1 : 0) +
            ",\"active_branches\":" + std::to_string(b ? 1 : 0) + ",\"a_tokens\":" + std::to_string(tokens) +
            ",\"b_shared_positions\":" + std::to_string(shared) + ",\"b_suffix_positions\":" + std::to_string(exclusive) +
            ",\"prediction_cached_tokens\":" + std::to_string(prediction_tokens.size()) +
            ",\"prediction_reused_tokens\":" + std::to_string(prediction_reused) +
            ",\"prediction_evaluated_tokens\":" + std::to_string(prediction_evaluated) +
            ",\"logical_unique_positions\":" + std::to_string(tokens + idle + exclusive + prediction_tokens.size()) +
            ",\"session_tokens\":" + std::to_string(resident ? resident->tokens.size() : 0) +
            ",\"session_reused_tokens\":" + std::to_string(session_reused) +
            ",\"session_evaluated_tokens\":" + std::to_string(session_evaluated) +
            ",\"session_loaded_tokens\":" + std::to_string(session_loaded) +
            ",\"session_loads\":" + std::to_string(session_loads) + ",\"session_saves\":" + std::to_string(session_saves) +
            ",\"session_save_failures\":" + std::to_string(session_save_failures) +
            ",\"fork_count\":" + std::to_string(forks) + ",\"release_count\":" + std::to_string(releases) +
            ",\"input_batch_max\":2,\"context_size\":" + std::to_string(context) + '}';
    }
};

static Api load_backends(const wchar_t* path) {
    auto root = std::filesystem::path(path).parent_path();
    auto load = [&](const std::filesystem::path& p) {
        return LoadLibraryExW(p.c_str(), nullptr, LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    };
    AddDllDirectory(root.c_str());
    auto ggml = load(root / L"ggml.dll"); require(ggml != nullptr, "ggml_load_failed");
    auto scan = reinterpret_cast<void (*)(const char*)>(GetProcAddress(ggml, "ggml_backend_load_all_from_path"));
    require(scan != nullptr, "ggml_backend_loader_missing"); scan(root.u8string().c_str());
    std::vector<std::filesystem::path> gpu;
    for (auto& entry : std::filesystem::directory_iterator(root))
        if (entry.is_directory() && entry.path().filename().wstring().find(L"cuda_v") == 0 &&
            std::filesystem::exists(entry.path() / L"ggml-cuda.dll")) gpu.push_back(entry.path());
    std::sort(gpu.rbegin(), gpu.rend());
    if (!gpu.empty()) { AddDllDirectory(gpu[0].c_str()); load(gpu[0] / L"ggml-cuda.dll"); scan(gpu[0].u8string().c_str()); }
    return Api::load(path);
}
}

// Small stable C ABI. Every result buffer belongs to the caller until echo_free.
// Errors are thread-local because HTTP calls may run on different worker threads.
#define EXPORT extern "C" __declspec(dllexport)
static thread_local std::string last_error;
template<class F> static int protect(F fn) noexcept {
    try { fn(); last_error.clear(); return 0; }
    catch (const std::exception& e) { last_error = e.what(); return -1; }
    catch (...) { last_error = "unknown_cpp_error"; return -1; }
}
static void output(char** result, const std::string& text) {
    auto buffer = std::make_unique<char[]>(text.size()+1);
    std::memcpy(buffer.get(), text.c_str(), text.size()+1); *result = buffer.release();
}
EXPORT int echo_abi() { return 2; }
EXPORT const char* echo_error() { return last_error.c_str(); }
EXPORT void echo_free(void* p) { delete[] static_cast<char*>(p); }
EXPORT int echo_open(const wchar_t* library, const char* model, int context, int gpu, void** result) {
    return protect([&] { *result = new echo::Runtime(echo::load_backends(library), model, context, gpu); });
}
EXPORT void echo_close(void* runtime) { delete static_cast<echo::Runtime*>(runtime); }
EXPORT int echo_begin(void* p, const char* text, uint32_t seed, bool sample, uint64_t* id, int* prefix) {
    return protect([&] { auto r = static_cast<echo::Runtime*>(p)->begin(text, seed, sample); *id = r.first; *prefix = r.second; });
}
EXPORT int echo_session_begin(void* p, const char* text, const char* path, uint64_t* id, int* prefix) {
    return protect([&] { auto r = static_cast<echo::Runtime*>(p)->begin_session(text, path); *id = r.first; *prefix = r.second; });
}
EXPORT int echo_session_flush(void* p) {
    return protect([&] { static_cast<echo::Runtime*>(p)->flush(); });
}
EXPORT int echo_step(void* p, uint64_t id, bool* done, char** text) {
    return protect([&] { auto r = static_cast<echo::Runtime*>(p)->step(id); *done = r.first; output(text, r.second); });
}
EXPORT int echo_tokenize(void* p, const char* text, bool special, char** result) {
    return protect([&] {
        auto ids = static_cast<echo::Runtime*>(p)->tokenize(text, special);
        std::string json = "[";
        for (size_t i=0; i<ids.size(); ++i) { if (i) json += ','; json += std::to_string(ids[i]); }
        output(result, json + ']');
    });
}
EXPORT int echo_prefix(void* p, uint64_t id, const char* text, int* prefix, char** remainder) {
    return protect([&] { auto r = static_cast<echo::Runtime*>(p)->prefix(id, text); *prefix = r.first; output(remainder, r.second); });
}
EXPORT int echo_fork(void* p, uint64_t id, int prefix, uint64_t* branch) {
    return protect([&] { *branch = static_cast<echo::Runtime*>(p)->fork(id, prefix); });
}
EXPORT int echo_select(void* p, uint64_t branch, const int32_t* suffix, int count,
                       const char* const* labels, int n, int budget, char** result, int* steps) {
    return protect([&] {
        echo::require(count > 0 && count <= 640 && n > 0 && n <= 256, "invalid_selection_input");
        std::vector<std::string> choices(labels, labels+n);
        auto r = static_cast<echo::Runtime*>(p)->select(branch, echo::Tokens(suffix, suffix+count), choices, budget);
        *steps = r.second; output(result, r.first);
    });
}
EXPORT int echo_release(void* p, uint64_t id, bool branch) {
    return protect([&] { auto r = static_cast<echo::Runtime*>(p); if (branch) r->release_b(id); else r->release_a(id); });
}
EXPORT int echo_predict(void* p, const char* prompt, int limit, char** result) {
    return protect([&] { output(result, static_cast<echo::Runtime*>(p)->predict(prompt, limit)); });
}
EXPORT int echo_select_batch(void* p, uint64_t source, int count,
                            const int32_t* suffix, const int* sizes,
                            const char* const* labels, const int* label_sizes, char** result) {
    return protect([&] {
        echo::require(count >= 1 && count <= 2, "invalid_batch_size");
        std::vector<echo::Tokens> tails; std::vector<std::vector<std::string>> choices;
        int ti = 0, li = 0;
        for (int j = 0; j < count; ++j) {
            echo::require(sizes[j] > 0 && sizes[j] <= 640 && label_sizes[j] > 0 && label_sizes[j] <= 64,
                          "invalid_selection_input");
            tails.emplace_back(suffix + ti, suffix + ti + sizes[j]); ti += sizes[j];
            choices.emplace_back(labels + li, labels + li + label_sizes[j]); li += label_sizes[j];
        }
        output(result, static_cast<echo::Runtime*>(p)->select_batch(source, tails, choices));
    });
}
EXPORT int echo_info(void* p, char** result) {
    return protect([&] { output(result, static_cast<echo::Runtime*>(p)->info()); });
}
