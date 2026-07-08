import axios from 'axios'
import type { TripFormData, TripPlan, TripPlanResponse } from '@/types'

export const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000'

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  timeout: 300000, // 5分钟超时,覆盖规划LLM与审核LLM的总耗时
  headers: {
    'Content-Type': 'application/json'
  }
})

// 请求拦截器
apiClient.interceptors.request.use(
  (config) => {
    console.log('发送请求:', config.method?.toUpperCase(), config.url)
    return config
  },
  (error) => {
    console.error('请求错误:', error)
    return Promise.reject(error)
  }
)

// 响应拦截器
apiClient.interceptors.response.use(
  (response) => {
    console.log('收到响应:', response.status, response.config.url)
    return response
  },
  (error) => {
    console.error('响应错误:', error.response?.status, error.message)
    return Promise.reject(error)
  }
)

/**
 * 生成旅行计划
 */
export async function generateTripPlan(formData: TripFormData): Promise<TripPlanResponse> {
  try {
    const response = await apiClient.post<TripPlanResponse>('/api/trip/plan', formData)
    return response.data
  } catch (error: any) {
    console.error('生成旅行计划失败:', error)
    throw new Error(error.response?.data?.detail || error.message || '生成旅行计划失败')
  }
}

export interface TripJobEvent {
  type: 'progress' | 'done' | 'failed'
  agent: string
  status: string
  message: string
  timestamp: string
  payload: Record<string, unknown>
  sequence: number
}

export interface TripJobCreateResponse {
  success: boolean
  message: string
  job_id: string
  status: string
}

export interface TripJobStatusResponse {
  success: boolean
  message: string
  job_id: string
  status: 'pending' | 'running' | 'succeeded' | 'failed'
  data?: TripPlan
  error?: string
  events: TripJobEvent[]
}

/**
 * 创建异步旅行规划任务
 */
export async function createTripJob(formData: TripFormData): Promise<TripJobCreateResponse> {
  try {
    const response = await apiClient.post<TripJobCreateResponse>('/api/trip/jobs', formData)
    return response.data
  } catch (error: any) {
    console.error('创建旅行规划任务失败:', error)
    throw new Error(error.response?.data?.detail || error.message || '创建旅行规划任务失败')
  }
}

/**
 * 获取异步旅行规划任务状态/结果
 */
export async function getTripJob(jobId: string): Promise<TripJobStatusResponse> {
  try {
    const response = await apiClient.get<TripJobStatusResponse>(`/api/trip/jobs/${jobId}`)
    return response.data
  } catch (error: any) {
    console.error('获取旅行规划任务失败:', error)
    throw new Error(error.response?.data?.detail || error.message || '获取旅行规划任务失败')
  }
}

/**
 * 创建任务事件流
 */
export function createTripJobEventSource(jobId: string): EventSource {
  return new EventSource(`${API_BASE_URL}/api/trip/jobs/${jobId}/events`)
}

/**
 * 健康检查
 */
export async function healthCheck(): Promise<any> {
  try {
    const response = await apiClient.get('/health')
    return response.data
  } catch (error: any) {
    console.error('健康检查失败:', error)
    throw new Error(error.message || '健康检查失败')
  }
}

export default apiClient
