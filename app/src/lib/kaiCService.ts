/**
 * Copyright (c) 2026 OpenNVR
 * This file is part of OpenNVR.
 * 
 * OpenNVR is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as published by
 * the Free Software Foundation, either version 3 of the License, or
 * (at your option) any later version.
 * 
 * OpenNVR is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 * 
 * You should have received a copy of the GNU Affero General Public License
 * along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.
 */

/**
 * KAI-C Service - Interface for connecting to AI Adapter servers
 * 
 * This service provides methods to:
 * - Connect to AI Adapter servers
 * - Fetch available models and tasks
 * - Send inference requests using KAI-C format
 */

export interface AIAdapterCapabilities {
  tasks: string[]
}

export interface AIAdapterSchema {
  task: string
  description?: string
  response_fields?: Record<string, any>
  example_response?: any
}

export interface KAIRequest {
  camera_id: string
  stream_url: string | number  // RTSP URL string or camera device ID (number)
  model_name: string           // e.g., "yolov8", "yolov11", "blip"
  task: string                 // e.g., "person_detection", "person_counting", "scene_description"
  options?: Record<string, any> // Additional options/parameters
}

export interface KAIResponse {
  event_type?: string
  camera_id?: string
  model_used?: string
  response?: any
  status?: string
  message?: string
}

class KaiCService {
  /**
   * Check if AI Adapter server is healthy
   */
  async checkAdapterHealth(adapterUrl: string): Promise<boolean> {
    try {
      const response = await fetch(`${adapterUrl}/health`, {
        method: 'GET',
        headers: { 'Accept': 'application/json' },
      })
      if (!response.ok) return false
      const data = await response.json()
      return data.status === 'ok'
    } catch (error) {
      console.error('Adapter health check failed:', error)
      return false
    }
  }

  /**
   * Fetch available tasks/capabilities from AI Adapter
   */
  async getCapabilities(adapterUrl: string): Promise<AIAdapterCapabilities> {
    try {
      const response = await fetch(`${adapterUrl}/capabilities`, {
        method: 'GET',
        headers: { 'Accept': 'application/json' },
      })
      if (!response.ok) {
        throw new Error(`Failed to fetch capabilities: ${response.statusText}`)
      }
      return await response.json()
    } catch (error) {
      console.error('Failed to fetch capabilities:', error)
      throw error
    }
  }

  /**
   * Get schema documentation for a specific task
   */
  async getTaskSchema(adapterUrl: string, task: string): Promise<AIAdapterSchema> {
    try {
      const response = await fetch(`${adapterUrl}/schema?task=${encodeURIComponent(task)}`, {
        method: 'GET',
        headers: { 'Accept': 'application/json' },
      })
      if (!response.ok) {
        throw new Error(`Failed to fetch schema: ${response.statusText}`)
      }
      return await response.json()
    } catch (error) {
      console.error('Failed to fetch schema:', error)
      throw error
    }
  }

  /**
   * Get all available schemas
   */
  async getAllSchemas(adapterUrl: string): Promise<Record<string, AIAdapterSchema>> {
    try {
      const response = await fetch(`${adapterUrl}/schema`, {
        method: 'GET',
        headers: { 'Accept': 'application/json' },
      })
      if (!response.ok) {
        throw new Error(`Failed to fetch schemas: ${response.statusText}`)
      }
      const data = await response.json()
      return data.schemas || {}
    } catch (error) {
      console.error('Failed to fetch schemas:', error)
      throw error
    }
  }

  /**
   * Send inference request to AI Adapter using KAI-C format
   * 
   * This method formats the request according to KAI-C connector specification
   * and sends it to the AI Adapter server.
   * 
   * For RTSP URLs, sends them directly. The adapter may need to handle
   * frame capture from RTSP streams, or a backend service can capture frames.
   */
  async processStream(
    adapterUrl: string,
    request: KAIRequest
  ): Promise<KAIResponse> {
    try {
      // Determine URI format
      let frameUri: string
      if (typeof request.stream_url === 'number') {
        // Camera device ID - use kavach:// format
        frameUri = `kavach://frames/camera_${request.stream_url}/latest.jpg`
      } else if (request.stream_url.startsWith('rtsp://')) {
        // RTSP URL - send directly (adapter or backend needs to handle frame capture)
        frameUri = request.stream_url
      } else if (request.stream_url.startsWith('http://') || request.stream_url.startsWith('https://')) {
        // HTTP URL - send directly
        frameUri = request.stream_url
      } else {
        // Assume it's a file path or kavach:// URI
        frameUri = request.stream_url
      }

      // Format payload according to AI Adapter API specification
      const payload = {
        task: request.task,
        input: {
          frame: {
            uri: frameUri
          },
          params: request.options || {}
        }
      }

      const response = await fetch(`${adapterUrl}/infer`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Accept': 'application/json',
        },
        body: JSON.stringify(payload),
      })

      if (!response.ok) {
        const errorText = await response.text()
        return {
          status: 'error',
          message: `Adapter failed: ${errorText}`,
        }
      }

      const responseData = await response.json()

      return {
        event_type: 'INFERENCE_COMPLETE',
        camera_id: request.camera_id,
        model_used: request.model_name,
        response: responseData,
      }
    } catch (error: any) {
      return {
        status: 'error',
        message: error.message || 'Failed to process stream',
      }
    }
  }

  /**
   * Map model name to appropriate task name
   * Helper function to suggest tasks based on model selection
   */
  getSuggestedTasks(modelName: string): string[] {
    const modelTaskMap: Record<string, string[]> = {
      'yolov8': ['person_detection', 'person_counting'],
      'yolov11': ['person_counting'],
      'blip': ['scene_description'],
      'insightface': ['face_detection', 'face_recognition', 'face_verify', 'watchlist_check'],
    }
    return modelTaskMap[modelName.toLowerCase()] || []
  }

  /**
   * Extract model name from task
   * Helper function to suggest model based on task selection
   */
  getSuggestedModel(task: string): string {
    if (task.includes('person')) {
      return task.includes('counting') ? 'yolov11' : 'yolov8'
    }
    if (task.includes('scene') || task.includes('caption')) {
      return 'blip'
    }
    if (task.includes('face')) {
      return 'insightface'
    }
    return 'yolov8' // default
  }
}

export const kaiCService = new KaiCService()
