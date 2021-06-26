
#ifndef basicOps_H
#define basicOps_H

#include <stdio.h>
#include <iostream>
#include <unistd.h>
#include <assert.h>

#include <cuda_runtime_api.h>
#include <cuda_fp16.h>

#define CUDA_CHECK_RETURN(value) {                      \
  cudaError_t _m_cudaStat = value;                    \
  if (_m_cudaStat != cudaSuccess) {                   \
    fprintf(stderr, "Error %s at line %d in file %s\n",         \
        cudaGetErrorString(_m_cudaStat), __LINE__, __FILE__);   \
    exit(1);                              \
  } }

#define THREADS_PER_BLOCKS (512)

typedef enum Operations_t
{
	ksmul = 0,
} Operations_t;

typedef enum Optimizer_t
{
	ADAM = 0,
	MOMENTUM = 1,
} Optimizer_t;


template <typename T> void estimateQuantiles(T *A, float *code, float offset, int n);

void quantize(float *code, float *A, unsigned char *out, int n);
void dequantize(float *code, unsigned char *A, float *out, int n);

template<typename T, int OPTIMIZER> void optimizer_32bit(T* g, T* p, 
                float* state1, float* state2,
                float beta1, float beta2, float eps, float weight_decay,
                int step, float lr, const bool is_sparse, const float gnorm_scale, int n);

template<typename T, int OPTIMIZER> void optimizerStatic8bit(T* p, T* g, unsigned char* state1, unsigned char* state2,
                float beta1, float beta2,
                float eps, int step, float lr, 
                float* quantiles1, float* quantiles2,
                float* max1, float* max2, float* new_max1, float* new_max2,
                float weight_decay,
                const float gnorm_scale, int n);

template<typename T> void percentileClipping(T * g, float *gnorm_vec, int step, const int n);

#endif







