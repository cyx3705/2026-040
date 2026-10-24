// Exercise the production C++ Runtime with a deterministic, in-memory llama ABI.
// No GPU/model download. Live acceptance separately covers the actual bundled DLL.
#include "echo_kernel.cpp"
#include <functional>
#include <iostream>
#include <limits>
#include <map>

using namespace echo;
namespace {
struct Fake {
    int positions[3] = {-1, -1, -1};
    int sampler_frees = 0, calls = 0, copies = 0;
    bool fail_release = false;
    std::vector<float> scores = std::vector<float>(256, -100.f);
    std::vector<std::tuple<int, int, int, bool>> batch;
    std::vector<std::vector<std::tuple<int, int, int, bool>>> processed;
    std::map<int, int> kv[3];
    std::vector<std::vector<float>> rows;
    std::map<int, std::vector<float>> seq_scores;
    std::map<std::string, Tokens> spelling;
    std::vector<int> sampled{1, 2, 250};
    std::map<int, std::string> pieces{{1, "the "}, {2, "banking"}};
    std::function<void()> on_process;
};
Fake* f;
struct Sampler { size_t cursor = 0; };

Api fake_api() {
    Api p;
    p.backend_init = []{}; p.backend_free = []{};
    p.model_default_params = []{return ModelParams{};};
    p.context_default_params = []{return ContextParams{};};
    p.model_load_from_file = [](const char*, ModelParams)->void* {return f;};
    p.model_free = [](void*){};
    p.model_get_vocab = [](void*)->void* {return f;};
    p.init_from_model = [](void*, ContextParams cp)->void* {
        require(cp.kv_unified && cp.n_seq_max == 3, "test:unified_pool"); return f;
    };
    p.free = [](void*){}; p.get_memory = [](void*)->void* {return f;};
    p.vocab_n_tokens = [](void*)->int32_t {return 256;};
    p.tokenize = [](void*, const char* s, int32_t n, int32_t* out, int32_t capacity, bool, bool)->int32_t {
        std::string text(s, n); auto it = f->spelling.find(text);
        Tokens ids = it == f->spelling.end() ? Tokens(text.begin(), text.end()) : it->second;
        if (int(ids.size()) > capacity) return -int(ids.size());
        std::copy(ids.begin(), ids.end(), out); return int(ids.size());
    };
    p.token_to_piece = [](void*, int32_t token, char* out, int32_t capacity, int32_t, bool)->int32_t {
        auto text = f->pieces.count(token) ? f->pieces[token] : std::string(1, char(token));
        if (int(text.size()) > capacity) return -int(text.size());
        std::memcpy(out, text.data(), text.size()); return int(text.size());
    };
    p.vocab_is_eog = [](void*, int32_t t) {return t == 250;};
    p.batch_ext_init = [](void*)->void* {return f;};
    p.batch_ext_free = [](void*){}; p.batch_ext_clear = [](void*) {f->batch.clear();};
    p.batch_ext_add_token = [](void*, int32_t seq, int32_t token)->int32_t {
        f->batch.emplace_back(seq, token, -1, true); return int(f->batch.size()) - 1;
    };
    p.batch_ext_set_pos = [](void*, int32_t i, const int32_t* pos) {
        std::get<2>(f->batch[i]) = *pos; return true;
    };
    p.batch_ext_set_output_logits = [](void*, int32_t i, bool flag) {
        std::get<3>(f->batch[i]) = flag; return true;
    };
    p.process = [](void*, int32_t, void*)->int32_t {
        ++f->calls; f->processed.push_back(f->batch); f->rows.clear();
        for (auto [seq, token, pos, flag] : f->batch) {
            f->positions[seq] = pos; f->kv[seq][pos] = token;
        }
        if (f->on_process) f->on_process();
        for (auto [seq, token, pos, flag] : f->batch)
            if (flag) f->rows.push_back(f->seq_scores.count(seq) ? f->seq_scores[seq] : f->scores);
        return 0;
    };
    p.get_logits_ith = [](void*, int32_t i)->float* {
        require(i < 0 && size_t(-i) <= f->rows.size(), "test:final_logits_row");
        return f->rows[f->rows.size() + i].data();
    };
    p.memory_seq_cp = [](void*, int32_t, int32_t target, int32_t start, int32_t end) {
        require(start == 0, "test:copy_start"); f->positions[target] = end - 1; ++f->copies;
    };
    p.memory_seq_rm = [](void*, int32_t seq, int32_t start, int32_t end) {
        if (f->fail_release) return false;
        auto& kv = f->kv[seq];
        kv.erase(kv.lower_bound(std::max(0, start)), end < 0 ? kv.end() : kv.lower_bound(end));
        f->positions[seq] = kv.empty() ? -1 : kv.rbegin()->first;
        return true;
    };
    p.memory_seq_pos_max = [](void*, int32_t seq)->int32_t {return f->positions[seq];};
    p.sampler_chain_default_params = []{return SamplerParams{};};
    p.sampler_chain_init = [](SamplerParams)->void* {return new Sampler;};
    p.sampler_chain_add = [](void*, void* stage) {delete static_cast<Sampler*>(stage);};
    p.sampler_init_top_k = [](int32_t)->void* {return new Sampler;};
    p.sampler_init_temp = [](float)->void* {return new Sampler;};
    p.sampler_init_top_p = [](float, size_t)->void* {return new Sampler;};
    p.sampler_init_dist = [](uint32_t)->void* {return new Sampler;};
    p.sampler_sample = [](void* ptr, void*, int32_t)->int32_t {
        auto sampler = static_cast<Sampler*>(ptr);
        return f->sampled[std::min(sampler->cursor++, f->sampled.size()-1)];
    };
    p.sampler_free = [](void* ptr) {++f->sampler_frees; delete static_cast<Sampler*>(ptr);};
    return p;
}
template<class F> void fails(const char* expected, F fn) {
    try { fn(); } catch (const std::runtime_error& e) {
        require(std::string(e.what()) == expected, e.what()); return;
    }
    throw std::runtime_error(std::string("expected error: ") + expected);
}
template<class F> void test(const char* name, F fn) {
    Fake state; f = &state; fn(); std::cout << "PASS " << name << '\n';
}
}

int main() {
    try {
        test("input KV batches distinct logits and shares prefix without generation", [] {
            Runtime r(fake_api(), "model", 4096, 0);
            auto [source, prefix] = r.begin("webpage", 1, false);
            auto original = f->kv[0]; int calls = f->calls;
            f->seq_scores[1] = f->scores; f->seq_scores[1]['A'] = 7;
            f->seq_scores[2] = f->scores; f->seq_scores[2]['B'] = 8;
            auto result = r.select_batch(source, {{4, 5}, {6, 7}}, {{"0", "A", "B"}, {"0", "A", "B"}});
            require(result == "[{\"label\":\"A\",\"decision_steps\":1},{\"label\":\"B\",\"decision_steps\":1}]", "test:independent_rows");
            require(f->calls == calls + 1 && f->copies == 2, "test:one_batched_forward");
            require(f->kv[0] == original && !r.a->sampler && r.a->generated.empty(), "test:input_only");
            require(f->positions[1] == -1 && f->positions[2] == -1 && r.forks == r.releases, "test:batch_release");
            // Uneven suffixes finish in different batches; retain both decisions.
            result = r.select_batch(source, {{4}, Tokens(130, 5)}, {{"0", "A"}, {"0", "B"}});
            require(result.find("\"B\"") != std::string::npos, "test:uneven_suffix");
            r.release_a(source);
        });
        test("input batches validate labels and pool before allocating", [] {
            Runtime r(fake_api(), "model", 4096, 0); auto [source, prefix] = r.begin("page", 1, false);
            fails("single_token_labels_required", [&]{r.select_batch(source, {{1}}, {{"AB"}});});
            fails("batch_context_limit", [&]{r.select_batch(source, {Tokens(400, 1), Tokens(400, 2)}, {{"A"}, {"B"}});});
            require(r.forks == 0 && !r.b, "test:no_invalid_forks"); r.release_a(source);
            auto [sampled, _] = r.begin("chat", 1, true);
            fails("input_sequence_required", [&]{r.select_batch(sampled, {{1}}, {{"A"}});});
            r.release_a(sampled);
        });
        test("input batch failure reclaims both branches and allows reuse", [] {
            Runtime r(fake_api(), "model", 4096, 0); auto [source, prefix] = r.begin("page", 1, false);
            f->on_process = []{throw std::runtime_error("injected_failure");};
            fails("injected_failure", [&]{r.select_batch(source, {{1}, {2}}, {{"A"}, {"B"}});});
            require(f->positions[1] == -1 && f->positions[2] == -1 && r.forks == r.releases, "test:failed_batch_release");
            f->on_process = {}; f->scores['A'] = 1;
            require(r.select_batch(source, {{1}}, {{"A"}}).find("\"A\"") != std::string::npos, "test:reuse_after_failure");
            r.release_a(source);
        });
        test("A sampler survives B logits and releases once", [] {
            Runtime r(fake_api(), "model", 4096, 0);
            auto [a, prefix] = r.begin("prompt", 73, true);
            require(r.step(a).second == "the ", "test:A_first");
            auto b = r.fork(a, prefix); f->scores['A'] = 9;
            require(r.select(b, {9}, {"0", "A"}, 8).first == "A", "test:B_choice");
            require(!r.b && f->positions[1] == -1, "test:B_eager_release");
            r.release_b(b);
            require(r.step(a).second == "banking", "test:A_after_B");
            require(r.step(a).first, "test:A_eog");
            r.release_a(a); r.release_a(a);
            require(f->sampler_frees == 1 && r.forks == r.releases, "test:release_once");
        });
        test("safe shared boundary and Unicode offsets", [] {
            Runtime r(fake_api(), "model", 4096, 0);
            auto [a, prefix] = r.begin("prompt", 73, true);
            r.step(a); r.step(a);
            require(r.prefix(a, "the bank") == std::make_pair(prefix+1, std::string("bank")), "test:cross_token");
            fails("prefix_text_mismatch", [&]{r.prefix(a, "The bank");});
            auto b = r.fork(a, prefix+1);
            require(f->positions[1] == prefix, "test:half_open_prefix"); r.release_b(b); r.release_a(a);
            f->sampled = {3, 4, 2, 250}; f->pieces[3] = "\xe4"; f->pieces[4] = "\xbd\xa0 ";
            auto [next, initial] = r.begin("prompt", 73, true);
            require(r.step(next).second.empty(), "test:split_utf8");
            require(r.step(next).second == "你 ", "test:complete_utf8");
            r.step(next);
            require(r.prefix(next, "你 bank") == std::make_pair(initial+2, std::string("bank")), "test:utf8_prefix");
        });
        test("final logits only, prefill split and nonfinite constraints", [] {
            Runtime r(fake_api(), "model", 4096, 0);
            auto [a, prefix] = r.begin("prompt", 0, false); auto b = r.fork(a, prefix);
            f->scores['A'] = 5;
            auto result = r.select(b, Tokens(130, 7), {"0", "A"}, 8);
            require(result.first == "A", "test:single_choice");
            auto& blocks = f->processed;
            require(blocks[1].size() == 64 && blocks[2].size() == 64 && blocks[3].size() == 2, "test:chunk64");
            for (size_t i = 1; i < blocks.size(); ++i)
                for (size_t j = 0; j < blocks[i].size(); ++j)
                    require(std::get<3>(blocks[i][j]) == (i+1 == blocks.size() && j+1 == blocks[i].size()), "test:final_output");
            r.release_b(b); b = r.fork(a, prefix);
            f->scores['A'] = std::numeric_limits<float>::quiet_NaN();
            fails("nonfinite_label_logits", [&]{r.select(b, {7}, {"0", "A"}, 8);});
        });
        test("canonical labels, finite trie and ambiguous mapping", [] {
            Runtime r(fake_api(), "model", 4096, 0);
            auto [a, prefix] = r.begin("prompt", 0, false);
            f->spelling = {{"A", {1}}, {" A", {2}}, {"0", {3}}, {" 0", {4}}};
            f->scores[1] = 4; f->scores[2] = 100; f->scores[3] = 1;
            auto b = r.fork(a, prefix);
            require(r.select(b, {7}, {"0", "A"}, 8).first == "A", "test:canonical_boundary");
            r.release_b(b); b = r.fork(a, prefix);
            f->spelling["B"] = {1};
            fails("ambiguous_label_tokenization", [&]{r.select(b, {7}, {"A", "B"}, 8);});
            r.release_b(b); b = r.fork(a, prefix);
            f->spelling.clear(); f->spelling["AB"] = {1, 2}; f->spelling["AC"] = {1, 3};
            f->spelling["AB\n"] = {1, 2, 10}; f->spelling["AC\n"] = {1, 3, 10};
            f->spelling["0\n"] = {4, 10}; f->scores.assign(256, -100); f->scores[0] = 999;
            f->scores[1] = 5; f->scores[2] = 4;
            require(r.select(b, {7}, {"AB", "AC", "0"}, 8) == std::make_pair(std::string("AB"), 2), "test:trie_steps");
            r.release_b(b); b = r.fork(a, prefix);
            f->scores[2] = std::numeric_limits<float>::infinity();
            fails("nonfinite_label_logits", [&]{r.select(b, {7}, {"AB", "AC", "0"}, 8);});
        });
        test("cancellation after final prefill cannot write replacement B", [] {
            Runtime r(fake_api(), "model", 4096, 0);
            auto [a, prefix] = r.begin("prompt", 0, false); auto old = r.fork(a, prefix);
            uint64_t next = 0;
            f->on_process = [&] { r.release_b(old); next = r.fork(a, prefix); };
            fails("branch_cancelled", [&]{r.select(old, {7}, {"0", "A"}, 8);});
            require(r.b && r.b->id == next && f->positions[1] == prefix-1, "test:replacement_intact");
            f->on_process = nullptr;
        });
        test("stale handles and bounded KV lifecycle", [] {
            Runtime r(fake_api(), "model", 4096, 0);
            auto old = r.begin("prompt", 0, false).first; r.release_a(old);
            auto [a, prefix] = r.begin("prompt", 0, false); r.release_a(old);
            require(r.a && r.a->id == a, "test:old_A_release");
            fails("sequence_unavailable", [&]{r.fork(old, 1);});
            for (int i=0; i<1000; ++i) {auto b=r.fork(a, 2); r.release_b(b); r.release_b(b);}
            r.release_a(a);
            require(!r.a && !r.b && r.forks == 1000 && r.releases == 1000, "test:no_leak");
        });
        test("prediction ranks stable ties, caches bounded KV and handles bad logits", [] {
            Runtime r(fake_api(), "model", 4096, 0);
            f->scores['A'] = f->scores['B'] = 9;
            auto json = r.predict("prompt", 2);
            require(json.find("\"token\":\"A\"") < json.find("\"token\":\"B\""), "test:top_ties");
            require(!r.a && !r.b && r.prediction_tokens.size() == 6, "test:idle_predict_cache");
            int calls = f->calls;
            require(r.predict("prompt", 2) == json && f->calls == calls, "test:identical_no_forward");
            r.predict("prompt", 20);
            require(f->calls == calls, "test:changing_k_no_forward");
            f->scores[5] = std::numeric_limits<float>::quiet_NaN();
            fails("nonfinite_logits", [&]{r.predict("changed", 20);});
            require(!r.a && r.prediction_tokens.empty() && f->positions[0] == -1, "test:predict_error_release");
            fails("invalid_top_k", [&]{r.predict("prompt", 21);});
        });
        test("prediction token LCP, deletion and replacement match cold evaluation", [] {
            Runtime r(fake_api(), "model", 4096, 0);
            f->spelling = {{"abc", {10,11,12}}, {"abcd", {10,20}},
                           {"abcde", {10,20,21}}, {"ab", {10}}, {"other", {30,31}}};
            f->on_process = [&] {
                int sum = 0; for (auto [pos, token] : f->kv[0]) sum += token;
                f->scores['A'] = float(sum % 7); f->scores['B'] = float(sum % 11);
            };
            for (auto text : {"abc", "abcd", "abcde", "ab", "other"}) {
                auto expected = f->spelling[text];
                auto warm = r.predict(text, 20);
                require(f->kv[0].size() == expected.size(), "test:no_stale_tail");
                for (size_t i=0; i<expected.size(); ++i) require(f->kv[0][int(i)] == expected[i], "test:token_lcp");
                if (std::string(text) == "abcd")
                    require(r.prediction_reused == 1 && r.prediction_evaluated == 1, "test:retokenized_tail");
                // Fresh evaluation must reproduce the cached ranking/probabilities.
                r.discard_prediction(); auto cold = r.predict(text, 20);
                require(warm == cold, "test:warm_equals_cold");
            }
        });
        test("prediction relinquishes KV to A and expires without invalidating handles", [] {
            Runtime r(fake_api(), "model", 4096, 0);
            r.predict("cached long prefix", 20);
            auto [a, prefix] = r.begin("A", 73, true);
            require(r.prediction_tokens.empty() && f->kv[0].size() == 1, "test:A_full_budget");
            fails("only_one_A_sequence_supported", [&]{r.predict("other", 20);});
            auto b = r.fork(a, prefix);
            r.release_b(b); r.release_a(a);
            r.predict("cached", 20); r.release_a(a);
            require(!r.prediction_tokens.empty(), "test:stale_A_no_effect");
            r.prediction_touched -= std::chrono::seconds(181); r.expire();
            require(r.prediction_tokens.empty() && f->positions[0] == -1, "test:idle_expiry");
            r.predict("cached", 20); f->fail_release = true;
            fails("prediction_release_failed", [&]{r.begin("A", 0, false);});
            require(!r.a && r.prediction_scores.empty(), "test:failed_cleanup_not_reused");
            f->fail_release = false; a = r.begin("A", 0, false).first;
            require(f->kv[0].size() == 1, "test:cleanup_retry"); r.release_a(a);
        });
        test("context and suffix budgets", [] {
            Runtime r(fake_api(), "model", 1024, 0);
            fails("context_limit", [&]{r.begin(std::string(257, 'x'), 0, true);});
            auto [a, prefix] = r.begin("prompt", 0, false); auto b = r.fork(a, prefix);
            fails("suffix_budget", [&]{r.select(b, Tokens(641, 7), {"A"}, 8);});
            fails("invalid_token_id", [&]{r.select(b, {-1}, {"A"}, 8);});
            f->fail_release = true;
            fails("branch_release_failed", [&]{r.release_b(b);});
            require(r.b && r.releases == 0, "test:failed_release_retains_handle");
            f->fail_release = false;
            r.release_a(a); require(!r.b && r.forks == r.releases, "test:release_cascade");
            f->sampled = {-1}; a = r.begin("prompt", 0, true).first;
            fails("invalid_sampled_token", [&]{r.step(a);});
        });
        test("input translation selects one space-prefixed label without generation", [] {
            Runtime r(fake_api(), "model", 4096, 0);
            f->spelling[" A"] = {17}; f->spelling[" B"] = {18};
            f->scores[19] = 100; // forbidden output must never win
            f->scores[17] = 3; f->scores[18] = 9;
            auto [a, prefix] = r.begin("translation prompt", 0, false);
            auto b = r.fork(a, prefix);
            int before = f->calls;
            auto result = r.select(b, {8, 9}, {"0", " A", " B"}, 1);
            require(result.first == " B" && result.second == 1, "test:one_translation_decision");
            require(f->calls == before + 1, "test:no_autoregressive_translation");
            require(!r.b && f->positions[1] == -1, "test:translation_branch_released");
            r.release_a(a);
        });
        // Fixed-work CPU benchmark, comparable with heapq on the same float32 data.
        std::vector<float> scores(151936);
        for (size_t i=0; i<scores.size(); ++i) scores[i] = float(std::sin(double(i)*.01));
        std::vector<double> ms;
        for (int i=0; i<30; ++i) {
            auto start = Clock::now(); auto ids = top_ids(scores.data(), int(scores.size()), 20);
            require(ids.size() == 20, "test:benchmark_result");
            ms.push_back(std::chrono::duration<double, std::milli>(Clock::now()-start).count());
        }
        std::sort(ms.begin(), ms.end());
        std::cout << "CPP_TOP20_151936_MEDIAN_MS " << ms[15] << '\n';
        return 0;
    } catch (const std::exception& e) { std::cerr << "FAIL " << e.what() << '\n'; return 1; }
}
