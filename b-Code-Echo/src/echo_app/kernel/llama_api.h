#pragma once
// ABI of Echo's bundled Ollama libllama extended-batch build. No third-party
// sources/binaries are modified. A different ABI must be verified before use.
#include <cstdint>
#include <cstddef>
#include <stdexcept>
#include <string>
#define NOMINMAX
#include <windows.h>

namespace echo {
struct ModelParams {
    void *devices, *tensor_buft_overrides;
    int32_t n_gpu_layers, split_mode, load_mode, lazy_mode, main_gpu;
    void *tensor_split, *progress_callback, *progress_callback_user_data, *kv_overrides;
    bool vocab_only, check_tensors, use_extra_bufts, no_host, no_alloc, load_mtp;
};
struct ContextParams {
    uint32_t n_ctx, n_batch, n_ubatch, n_seq_max, n_rs_seq, n_outputs_max, n_outputs_max_per_seq;
    int32_t n_threads, n_threads_batch, ctx_type, rope_scaling_type, pooling_type,
            attention_type, flash_attn_type;
    float rope_freq_base, rope_freq_scale, yarn_ext_factor, yarn_attn_factor, yarn_beta_fast, yarn_beta_slow;
    uint32_t yarn_orig_ctx;
    float defrag_thold;
    void *cb_eval, *cb_eval_user_data;
    int32_t type_k, type_v;
    size_t moe_cache_size;
    void *abort_callback, *abort_callback_data;
    bool embeddings, offload_kqv, no_perf, op_offload, swa_full, kv_unified;
    void *samplers;
    size_t n_samplers;
    void *ctx_other;
};
struct SamplerParams { bool no_perf; };
static_assert(sizeof(ModelParams) == 80 && sizeof(ContextParams) == 168,
              "Echo requires the Windows x64 bundled llama ABI");

#define ECHO_LLAMA_API(X) \
    X(void, backend_init, ()) \
    X(void, backend_free, ()) \
    X(ModelParams, model_default_params, ()) \
    X(ContextParams, context_default_params, ()) \
    X(void*, model_load_from_file, (const char*, ModelParams)) \
    X(void, model_free, (void*)) \
    X(void*, model_get_vocab, (void*)) \
    X(void*, init_from_model, (void*, ContextParams)) \
    X(void, free, (void*)) \
    X(void*, get_memory, (void*)) \
    X(int32_t, vocab_n_tokens, (void*)) \
    X(int32_t, token_to_piece, (void*, int32_t, char*, int32_t, int32_t, bool)) \
    X(bool, vocab_is_eog, (void*, int32_t)) \
    X(int32_t, tokenize, (void*, const char*, int32_t, int32_t*, int32_t, bool, bool)) \
    X(void*, batch_ext_init, (void*)) \
    X(void, batch_ext_free, (void*)) \
    X(void, batch_ext_clear, (void*)) \
    X(int32_t, batch_ext_add_token, (void*, int32_t, int32_t)) \
    X(bool, batch_ext_set_pos, (void*, int32_t, const int32_t*)) \
    X(bool, batch_ext_set_output_logits, (void*, int32_t, bool)) \
    X(int32_t, process, (void*, int32_t, void*)) \
    X(float*, get_logits_ith, (void*, int32_t)) \
    X(void, memory_seq_cp, (void*, int32_t, int32_t, int32_t, int32_t)) \
    X(bool, memory_seq_rm, (void*, int32_t, int32_t, int32_t)) \
    X(int32_t, memory_seq_pos_max, (void*, int32_t))     X(size_t, state_seq_save_file, (void*, const char*, int32_t, const int32_t*, size_t))     X(size_t, state_seq_load_file, (void*, const char*, int32_t, int32_t*, size_t, size_t*)) \
    X(SamplerParams, sampler_chain_default_params, ()) \
    X(void*, sampler_chain_init, (SamplerParams)) \
    X(void, sampler_chain_add, (void*, void*)) \
    X(void*, sampler_init_top_k, (int32_t)) \
    X(void*, sampler_init_temp, (float)) \
    X(void*, sampler_init_top_p, (float, size_t)) \
    X(void*, sampler_init_dist, (uint32_t)) \
    X(int32_t, sampler_sample, (void*, void*, int32_t)) \
    X(void, sampler_free, (void*))

struct Api {
#define DECLARE(ret, name, args) ret (*name) args = nullptr;
    ECHO_LLAMA_API(DECLARE)
#undef DECLARE
    // Loaded once for the process; retain backends until process shutdown.
    static Api load(const std::wstring& library) {
        HMODULE module = LoadLibraryExW(library.c_str(), nullptr,
            LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
        if (!module) throw std::runtime_error("libllama_load_failed");
        Api api;
#define LOAD(ret, name, args) \
        api.name = reinterpret_cast<decltype(api.name)>(GetProcAddress(module, "llama_" #name)); \
        if (!api.name) throw std::runtime_error("missing_llama_symbol:" #name);
        ECHO_LLAMA_API(LOAD)
#undef LOAD
        return api;
    }
};
}
