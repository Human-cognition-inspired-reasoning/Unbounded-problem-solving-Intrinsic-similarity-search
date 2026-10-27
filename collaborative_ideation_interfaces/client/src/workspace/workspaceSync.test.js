import assert from 'node:assert/strict'
import test from 'node:test'
import {
  WORKSPACE_SYNC_CHANNEL,
  createRefreshScheduler,
  createWorkspaceChannel,
  mergeSyncedConversations,
  readStableSnapshot,
} from './workspaceSync.js'

test('a focus refresh crossing completion cannot restore the old generating state', async () => {
  let revision = 0
  let releaseStaleSnapshot
  let calls = 0
  const completed = { isGenerating: false, messages: ['saved answer'] }
  const pending = readStableSnapshot(() => {
    calls += 1
    return calls === 1
      ? new Promise((resolve) => { releaseStaleSnapshot = resolve })
      : Promise.resolve(completed)
  }, () => revision)

  revision += 1
  releaseStaleSnapshot({ isGenerating: true, messages: [] })

  assert.deepEqual(await pending, completed)
  assert.equal(calls, 2)
})

class FakeBroadcastChannel {
  static instances = []

  constructor(name) {
    this.name = name
    this.listeners = new Set()
    FakeBroadcastChannel.instances.push(this)
  }

  addEventListener(_type, listener) {
    this.listeners.add(listener)
  }

  removeEventListener(_type, listener) {
    this.listeners.delete(listener)
  }

  postMessage(message) {
    for (const instance of FakeBroadcastChannel.instances) {
      if (instance !== this && instance.name === this.name) {
        for (const listener of instance.listeners) listener({ data: message })
      }
    }
  }

  close() {
    FakeBroadcastChannel.instances = FakeBroadcastChannel.instances.filter((row) => row !== this)
  }
}

test('workspace invalidations cross tabs without echoing to the sender', () => {
  FakeBroadcastChannel.instances = []
  const firstEvents = []
  const secondEvents = []
  const first = createWorkspaceChannel((event) => firstEvents.push(event), FakeBroadcastChannel)
  const second = createWorkspaceChannel((event) => secondEvents.push(event), FakeBroadcastChannel)

  first.publish('generation_completed', 'conversation-1')

  assert.equal(FakeBroadcastChannel.instances[0].name, WORKSPACE_SYNC_CHANNEL)
  assert.equal(firstEvents.length, 0)
  assert.equal(secondEvents.length, 1)
  assert.equal(secondEvents[0].conversationId, 'conversation-1')
  first.close()
  second.close()
})

test('refresh scheduler coalesces a burst of invalidations', async () => {
  let refreshCount = 0
  const scheduler = createRefreshScheduler(async () => {
    refreshCount += 1
  }, 5)

  scheduler.schedule()
  scheduler.schedule()
  scheduler.schedule()
  await new Promise((resolve) => setTimeout(resolve, 30))

  assert.equal(refreshCount, 1)
  scheduler.close()
})

test('workspace merge refreshes the active body and preserves unchanged lazy-loaded bodies', () => {
  const current = [
    { id: 'active', messageCount: 1, updatedAt: '1', messages: [{ id: 'old' }] },
    { id: 'unchanged', messageCount: 2, updatedAt: '2', messages: [{ id: 'keep' }] },
    { id: 'changed', messageCount: 1, updatedAt: '3', messages: [{ id: 'drop' }] },
  ]
  const incoming = [
    { id: 'active', messageCount: 2, updatedAt: '4', isGenerating: false },
    { id: 'unchanged', messageCount: 2, updatedAt: '2', isGenerating: false },
    { id: 'changed', messageCount: 2, updatedAt: '5', isGenerating: true },
  ]
  const merged = mergeSyncedConversations(current, incoming, 'active', [{ id: 'fresh' }])

  assert.deepEqual(merged[0].messages, [{ id: 'fresh' }])
  assert.deepEqual(merged[1].messages, [{ id: 'keep' }])
  assert.equal('messages' in merged[2], false)
  assert.equal(merged[2].isGenerating, true)
})
