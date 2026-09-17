// gemm.cu---a minimal CUDA GEMM (general matrix multiply): C = A * B for N×N float matrices.
//
// Two kernels, so the classic CUDA lesson is visible side by side:
//   gemm_naive ---one thread per output element; each thread streams a full row of A and a full
//                 column of B from global memory (memory-bound: every value is re-read N times).
//   gemm_tiled ---same math, but threads in a block cooperatively stage TILE×TILE sub-blocks of A
//                 and B into shared memory, so each global value is read once per tile instead of
//                 N times. This is the standard shared-memory tiling optimization.
//
// Correctness is checked against a plain CPU reference (relative error); throughput is measured
// with CUDA events and reported as GFLOP/s (a GEMM performs 2*N^3 floating-point operations).
//
// Build:  nvcc -O3 gemm.cu -o gemm                 # default arch; runs on the GPU via PTX JIT
//         nvcc -O3 -arch=native gemm.cu -o gemm    # or compile straight for the local GPU
// Run:    ./gemm [N]                               # N = matrix dimension (default 512)

#include <cstdio>
#include <cstdlib>
#include <cmath>
#include <cuda_runtime.h>

#define TILE 16

// Abort with a message if any CUDA call fails---keeps the example honest about errors.
#define CUDA_CHECK(call)                                                            \
    do {                                                                            \
        cudaError_t err = (call);                                                   \
        if (err != cudaSuccess) {                                                   \
            fprintf(stderr, "CUDA error: %s at %s:%d\n",                            \
                    cudaGetErrorString(err), __FILE__, __LINE__);                   \
            exit(EXIT_FAILURE);                                                     \
        }                                                                           \
    } while (0)

// Naive: thread (row,col) computes C[row][col] = sum_k A[row][k] * B[k][col].
__global__ void gemm_naive(const float* A, const float* B, float* C, int N) {
    int row = blockIdx.y * blockDim.y + threadIdx.y;
    int col = blockIdx.x * blockDim.x + threadIdx.x;
    if (row < N && col < N) {
        float acc = 0.0f;
        for (int k = 0; k < N; ++k)
            acc += A[row * N + k] * B[k * N + col];
        C[row * N + col] = acc;
    }
}

// Tiled: each block walks the k dimension one TILE at a time, staging a TILE×TILE block of A and
// of B into shared memory, then multiplying from there. Every global element is fetched once per
// tile (not N times), which is where the speedup over the naive kernel comes from.
__global__ void gemm_tiled(const float* A, const float* B, float* C, int N) {
    __shared__ float As[TILE][TILE];
    __shared__ float Bs[TILE][TILE];

    int row = blockIdx.y * TILE + threadIdx.y;
    int col = blockIdx.x * TILE + threadIdx.x;
    float acc = 0.0f;

    for (int t = 0; t < (N + TILE - 1) / TILE; ++t) {
        int aCol = t * TILE + threadIdx.x;
        int bRow = t * TILE + threadIdx.y;
        As[threadIdx.y][threadIdx.x] = (row < N && aCol < N) ? A[row * N + aCol] : 0.0f;
        Bs[threadIdx.y][threadIdx.x] = (bRow < N && col < N) ? B[bRow * N + col] : 0.0f;
        __syncthreads();                        // wait until the whole tile is staged

        for (int k = 0; k < TILE; ++k)
            acc += As[threadIdx.y][k] * Bs[k][threadIdx.x];
        __syncthreads();                        // finish reading before the next tile overwrites it
    }
    if (row < N && col < N)
        C[row * N + col] = acc;
}

static void gemm_cpu(const float* A, const float* B, float* C, int N) {
    for (int i = 0; i < N; ++i)
        for (int j = 0; j < N; ++j) {
            float acc = 0.0f;
            for (int k = 0; k < N; ++k)
                acc += A[i * N + k] * B[k * N + j];
            C[i * N + j] = acc;
        }
}

int main(int argc, char** argv) {
    int N = (argc > 1) ? atoi(argv[1]) : 512;
    double flop = 2.0 * (double)N * N * N;
    printf("GEMM  C = A * B   (N = %d, %.1f MFLOP per multiply)\n", N, flop / 1e6);

    size_t bytes = (size_t)N * N * sizeof(float);
    float *hA = (float*)malloc(bytes), *hB = (float*)malloc(bytes);
    float *hC = (float*)malloc(bytes), *hRef = (float*)malloc(bytes);

    srand(1234);
    for (int i = 0; i < N * N; ++i) { hA[i] = (float)rand() / RAND_MAX; hB[i] = (float)rand() / RAND_MAX; }

    float *dA, *dB, *dC;
    CUDA_CHECK(cudaMalloc(&dA, bytes));
    CUDA_CHECK(cudaMalloc(&dB, bytes));
    CUDA_CHECK(cudaMalloc(&dC, bytes));
    CUDA_CHECK(cudaMemcpy(dA, hA, bytes, cudaMemcpyHostToDevice));
    CUDA_CHECK(cudaMemcpy(dB, hB, bytes, cudaMemcpyHostToDevice));

    dim3 block(TILE, TILE);
    dim3 grid((N + TILE - 1) / TILE, (N + TILE - 1) / TILE);
    cudaEvent_t t0, t1;
    CUDA_CHECK(cudaEventCreate(&t0));
    CUDA_CHECK(cudaEventCreate(&t1));
    const int iters = 10;
    float ms;

    // --- naive (warm-up once, then time `iters` launches) ---
    gemm_naive<<<grid, block>>>(dA, dB, dC, N);
    CUDA_CHECK(cudaGetLastError());
    CUDA_CHECK(cudaEventRecord(t0));
    for (int i = 0; i < iters; ++i) gemm_naive<<<grid, block>>>(dA, dB, dC, N);
    CUDA_CHECK(cudaEventRecord(t1));
    CUDA_CHECK(cudaEventSynchronize(t1));
    CUDA_CHECK(cudaEventElapsedTime(&ms, t0, t1)); ms /= iters;
    printf("  naive : %8.3f ms   %8.1f GFLOP/s\n", ms, flop / (ms * 1e6));

    // --- tiled ---
    gemm_tiled<<<grid, block>>>(dA, dB, dC, N);
    CUDA_CHECK(cudaGetLastError());
    CUDA_CHECK(cudaEventRecord(t0));
    for (int i = 0; i < iters; ++i) gemm_tiled<<<grid, block>>>(dA, dB, dC, N);
    CUDA_CHECK(cudaEventRecord(t1));
    CUDA_CHECK(cudaEventSynchronize(t1));
    CUDA_CHECK(cudaEventElapsedTime(&ms, t0, t1)); ms /= iters;
    printf("  tiled : %8.3f ms   %8.1f GFLOP/s\n", ms, flop / (ms * 1e6));

    // --- correctness: tiled GPU result vs CPU reference (relative max error) ---
    CUDA_CHECK(cudaMemcpy(hC, dC, bytes, cudaMemcpyDeviceToHost));
    gemm_cpu(hA, hB, hRef, N);
    float max_diff = 0.0f, max_ref = 0.0f;
    for (int i = 0; i < N * N; ++i) {
        max_diff = fmaxf(max_diff, fabsf(hC[i] - hRef[i]));
        max_ref  = fmaxf(max_ref, fabsf(hRef[i]));
    }
    float rel = max_diff / max_ref;
    printf("  check : rel_err = %.2e  ->  %s\n", rel, rel < 1e-3f ? "PASS" : "FAIL");

    cudaFree(dA); cudaFree(dB); cudaFree(dC);
    free(hA); free(hB); free(hC); free(hRef);
    return 0;
}
