import assert from 'node:assert/strict'
import test from 'node:test'
import { prepareAttachment } from './attachmentUpload.js'

for (const failedStage of ['upload', 'complete']) {
  test(`retry after ${failedStage} failure reuses the original attachment`, async () => {
    let draft = { file: { name: 'research.csv' } }
    const calls = []
    let failed = false
    const ready = { id: 'a1', status: 'ready', files: [{ name: 'research.csv' }] }
    const api = {
      async initAttachment() {
        calls.push('init')
        return { attachment: { id: 'a1', status: 'pending' }, upload: 'initial ticket' }
      },
      async renewAttachmentUpload(id) {
        assert.equal(id, 'a1')
        calls.push('renew')
        return { upload: 'renewed ticket' }
      },
      async uploadAttachment(ticket) {
        calls.push('upload')
        assert.equal(ticket, failed ? 'renewed ticket' : 'initial ticket')
        if (failedStage === 'upload' && !failed) {
          failed = true
          throw new Error('network failure')
        }
      },
      async completeAttachment(id) {
        assert.equal(id, 'a1')
        calls.push('complete')
        if (failedStage === 'complete' && !failed) {
          failed = true
          throw new Error('conversion failure')
        }
        return { attachment: ready }
      },
    }
    const patch = (changes) => { draft = { ...draft, ...changes } }
    await assert.rejects(prepareAttachment(draft, patch, api))
    assert.equal(draft.attachment.id, 'a1')
    assert.deepEqual(await prepareAttachment(draft, patch, api), ready)
    assert.deepEqual(calls, failedStage === 'upload'
      ? ['init', 'upload', 'renew', 'upload', 'complete']
      : ['init', 'upload', 'complete', 'complete'])

    // A rejected message send can retry without uploading or converting again.
    const previousCalls = [...calls]
    assert.deepEqual(await prepareAttachment(draft, patch, api), ready)
    assert.deepEqual(calls, previousCalls)
  })
}
