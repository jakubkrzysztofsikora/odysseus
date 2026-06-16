import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { CallToolRequestSchema, ListToolsRequestSchema } from '@modelcontextprotocol/sdk/types.js';

// In-memory storage for tasks
interface Task {
  id: string;
  title: string;
  description?: string;
  status: 'pending' | 'in_progress' | 'completed' | 'cancelled';
  createdAt: string;
  updatedAt: string;
  dueDate?: string;
  priority?: 'low' | 'medium' | 'high';
}

const tasks: Map<string, Task> = new Map();
let nextId = 1;

// Generate unique ID for tasks
function generateId(): string {
  return `task_${nextId++}`;
}

// Create the MCP server
const server = new Server(
  {
    name: 'todo-server',
    version: '1.0.0',
  },
  {
    capabilities: {
      tools: {},
    },
  }
);

// Tool: Create a new task
server.setRequestHandler(ListToolsRequestSchema, async () => {
  return {
    tools: [
      {
        name: 'todo_create_task',
        description: 'Create a new task in the todo list. Use this to add new items to your task management system.',
        inputSchema: {
          type: 'object',
          properties: {
            title: {
              type: 'string',
              description: 'The title or name of the task (required)',
            },
            description: {
              type: 'string',
              description: 'Detailed description of the task (optional)',
            },
            dueDate: {
              type: 'string',
              description: 'Due date for the task in ISO 8601 format (optional)',
            },
            priority: {
              type: 'string',
              enum: ['low', 'medium', 'high'],
              description: 'Priority level of the task (optional, defaults to medium)',
            },
          },
          required: ['title'],
        },
      },
      {
        name: 'todo_list_tasks',
        description: 'List all tasks with optional filtering. Returns a paginated list of tasks that you can filter by status, priority, or search term.',
        inputSchema: {
          type: 'object',
          properties: {
            status: {
              type: 'string',
              enum: ['pending', 'in_progress', 'completed', 'cancelled', 'all'],
              description: 'Filter tasks by status (optional, defaults to all)',
            },
            priority: {
              type: 'string',
              enum: ['low', 'medium', 'high', 'all'],
              description: 'Filter tasks by priority (optional, defaults to all)',
            },
            search: {
              type: 'string',
              description: 'Search term to filter tasks by title or description (optional)',
            },
            limit: {
              type: 'number',
              description: 'Maximum number of tasks to return (optional, defaults to 50)',
              minimum: 1,
              maximum: 100,
            },
            offset: {
              type: 'number',
              description: 'Number of tasks to skip for pagination (optional, defaults to 0)',
              minimum: 0,
            },
          },
        },
      },
      {
        name: 'todo_get_task',
        description: 'Get detailed information about a specific task by its ID. Use this when you need to see the full details of a particular task.',
        inputSchema: {
          type: 'object',
          properties: {
            taskId: {
              type: 'string',
              description: 'The unique identifier of the task to retrieve',
            },
          },
          required: ['taskId'],
        },
      },
      {
        name: 'todo_update_task',
        description: 'Update an existing task. Use this to modify task properties like title, description, status, priority, or due date.',
        inputSchema: {
          type: 'object',
          properties: {
            taskId: {
              type: 'string',
              description: 'The unique identifier of the task to update',
            },
            title: {
              type: 'string',
              description: 'New title for the task (optional)',
            },
            description: {
              type: 'string',
              description: 'New description for the task (optional)',
            },
            status: {
              type: 'string',
              enum: ['pending', 'in_progress', 'completed', 'cancelled'],
              description: 'New status for the task (optional)',
            },
            priority: {
              type: 'string',
              enum: ['low', 'medium', 'high'],
              description: 'New priority for the task (optional)',
            },
            dueDate: {
              type: 'string',
              description: 'New due date for the task in ISO 8601 format (optional)',
            },
          },
          required: ['taskId'],
        },
      },
      {
        name: 'todo_delete_task',
        description: 'Delete a task from the todo list. Use this to remove tasks that are no longer needed.',
        inputSchema: {
          type: 'object',
          properties: {
            taskId: {
              type: 'string',
              description: 'The unique identifier of the task to delete',
            },
          },
          required: ['taskId'],
        },
      },
      {
        name: 'todo_complete_task',
        description: 'Mark a task as completed. This is a convenience method that updates the task status to "completed".',
        inputSchema: {
          type: 'object',
          properties: {
            taskId: {
              type: 'string',
              description: 'The unique identifier of the task to mark as completed',
            },
          },
          required: ['taskId'],
        },
      },
    ],
  };
});

// Handle tool calls
server.setRequestHandler(CallToolRequestSchema, async (request) => {
  const { name, arguments: args } = request.params;

  try {
    switch (name) {
      case 'todo_create_task':
        return await createTask(args);
      case 'todo_list_tasks':
        return await listTasks(args);
      case 'todo_get_task':
        return await getTask(args);
      case 'todo_update_task':
        return await updateTask(args);
      case 'todo_delete_task':
        return await deleteTask(args);
      case 'todo_complete_task':
        return await completeTask(args);
      default:
        throw new Error(`Unknown tool: ${name}`);
    }
  } catch (error) {
    return {
      content: [
        {
          type: 'text',
          text: `Error: ${error instanceof Error ? error.message : String(error)}`,
        },
      ],
      isError: true,
    };
  }
});

// Tool implementations
async function createTask(args: any) {
  const { title, description, dueDate, priority = 'medium' } = args;

  if (!title || typeof title !== 'string' || title.trim() === '') {
    throw new Error('Title is required and must be a non-empty string');
  }

  const now = new Date().toISOString();
  const task: Task = {
    id: generateId(),
    title: title.trim(),
    description: description?.trim(),
    status: 'pending',
    priority: priority as 'low' | 'medium' | 'high',
    createdAt: now,
    updatedAt: now,
    dueDate: dueDate?.trim(),
  };

  tasks.set(task.id, task);

  return {
    content: [
      {
        type: 'text',
        text: `Task created successfully with ID: ${task.id}`,
      },
      {
        type: 'text',
        text: JSON.stringify(task, null, 2),
      },
    ],
  };
}

async function listTasks(args: any) {
  const {
    status = 'all',
    priority = 'all',
    search = '',
    limit = 50,
    offset = 0,
  } = args;

  let filteredTasks = Array.from(tasks.values());

  // Apply filters
  if (status !== 'all') {
    filteredTasks = filteredTasks.filter((task) => task.status === status);
  }

  if (priority !== 'all') {
    filteredTasks = filteredTasks.filter((task) => task.priority === priority);
  }

  if (search) {
    const searchLower = search.toLowerCase();
    filteredTasks = filteredTasks.filter(
      (task) =>
        task.title.toLowerCase().includes(searchLower) ||
        (task.description?.toLowerCase().includes(searchLower) ?? false)
    );
  }

  // Apply pagination
  const total = filteredTasks.length;
  const paginatedTasks = filteredTasks.slice(offset, offset + limit);

  return {
    content: [
      {
        type: 'text',
        text: `Found ${total} tasks (showing ${paginatedTasks.length} from offset ${offset})`,
      },
      {
        type: 'text',
        text: JSON.stringify(paginatedTasks, null, 2),
      },
    ],
  };
}

async function getTask(args: any) {
  const { taskId } = args;

  if (!taskId || typeof taskId !== 'string') {
    throw new Error('taskId is required and must be a string');
  }

  const task = tasks.get(taskId);

  if (!task) {
    throw new Error(`Task with ID ${taskId} not found`);
  }

  return {
    content: [
      {
        type: 'text',
        text: JSON.stringify(task, null, 2),
      },
    ],
  };
}

async function updateTask(args: any) {
  const { taskId, ...updates } = args;

  if (!taskId || typeof taskId !== 'string') {
    throw new Error('taskId is required and must be a string');
  }

  const task = tasks.get(taskId);

  if (!task) {
    throw new Error(`Task with ID ${taskId} not found`);
  }

  // Validate updates
  const validUpdates: Partial<Task> = {};

  if (updates.title !== undefined) {
    if (typeof updates.title !== 'string' || updates.title.trim() === '') {
      throw new Error('Title must be a non-empty string');
    }
    validUpdates.title = updates.title.trim();
  }

  if (updates.description !== undefined) {
    validUpdates.description = updates.description?.trim();
  }

  if (updates.status !== undefined) {
    const validStatuses = ['pending', 'in_progress', 'completed', 'cancelled'];
    if (!validStatuses.includes(updates.status)) {
      throw new Error(
        `Invalid status. Must be one of: ${validStatuses.join(', ')}`
      );
    }
    validUpdates.status = updates.status;
  }

  if (updates.priority !== undefined) {
    const validPriorities = ['low', 'medium', 'high'];
    if (!validPriorities.includes(updates.priority)) {
      throw new Error(
        `Invalid priority. Must be one of: ${validPriorities.join(', ')}`
      );
    }
    validUpdates.priority = updates.priority;
  }

  if (updates.dueDate !== undefined) {
    validUpdates.dueDate = updates.dueDate?.trim();
  }

  // Apply updates
  const updatedTask = { ...task, ...validUpdates, updatedAt: new Date().toISOString() };
  tasks.set(taskId, updatedTask);

  return {
    content: [
      {
        type: 'text',
        text: `Task ${taskId} updated successfully`,
      },
      {
        type: 'text',
        text: JSON.stringify(updatedTask, null, 2),
      },
    ],
  };
}

async function deleteTask(args: any) {
  const { taskId } = args;

  if (!taskId || typeof taskId !== 'string') {
    throw new Error('taskId is required and must be a string');
  }

  if (!tasks.has(taskId)) {
    throw new Error(`Task with ID ${taskId} not found`);
  }

  tasks.delete(taskId);

  return {
    content: [
      {
        type: 'text',
        text: `Task ${taskId} deleted successfully`,
      },
    ],
  };
}

async function completeTask(args: any) {
  const { taskId } = args;

  if (!taskId || typeof taskId !== 'string') {
    throw new Error('taskId is required and must be a string');
  }

  const task = tasks.get(taskId);

  if (!task) {
    throw new Error(`Task with ID ${taskId} not found`);
  }

  const updatedTask = { ...task, status: 'completed' as const, updatedAt: new Date().toISOString() };
  tasks.set(taskId, updatedTask);

  return {
    content: [
      {
        type: 'text',
        text: `Task ${taskId} marked as completed`,
      },
      {
        type: 'text',
        text: JSON.stringify(updatedTask, null, 2),
      },
    ],
  };
}

// Start the server
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch(console.error);
