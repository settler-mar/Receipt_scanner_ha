<script setup lang="ts">
import { ref } from 'vue'

defineOptions({ inheritAttrs: false })

const props = defineProps<{ modelValue: string }>()
const emit = defineEmits<{ 'update:modelValue': [value: string] }>()

const visible = ref(false)
const EN_LAYOUT = "`qwertyuiop[]asdfghjkl;'zxcvbnm,."
const UA_LAYOUT = 'ґйцукенгшщзхїфівапролджєячсмитьбю'

function translateLayout(from: string, to: string) {
  const table = new Map<string, string>()
  const source = Array.from(from)
  const target = Array.from(to)
  source.forEach((character, index) => {
    // The two layout strings are written to the same length; a character with no
    // counterpart is simply left alone rather than mapped to undefined.
    const replacement = target[index]
    if (replacement === undefined) return
    table.set(character, replacement)
    if (character.toUpperCase() !== character) {
      table.set(character.toUpperCase(), replacement.toUpperCase())
    }
  })
  emit('update:modelValue', Array.from(props.modelValue).map((character) => table.get(character) ?? character).join(''))
}
</script>

<template>
  <div class="password-input">
    <input
      v-bind="$attrs"
      :value="modelValue"
      :type="visible ? 'text' : 'password'"
      class="password-input__control"
      @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
    />
    <button
      type="button"
      class="secondary password-input__button password-input__eye"
      :title="visible ? 'Приховати пароль' : 'Показати пароль'"
      :aria-label="visible ? 'Приховати пароль' : 'Показати пароль'"
      @click="visible = !visible"
    >
      <svg v-if="!visible" viewBox="0 0 24 24" aria-hidden="true">
        <path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12Z" />
        <circle cx="12" cy="12" r="3" />
      </svg>
      <svg v-else viewBox="0 0 24 24" aria-hidden="true">
        <path d="M3 3l18 18M10.6 6.2A9 9 0 0 1 12 6c6.5 0 10 6 10 6a16 16 0 0 1-3 3.7M6.5 6.5C3.6 8.2 2 12 2 12s3.5 6 10 6c1 0 2-.2 2.8-.4M9.9 9.9a3 3 0 0 0 4.2 4.2" />
      </svg>
    </button>
    <button
      type="button"
      class="secondary password-input__button"
      title="Перетворити текст, набраний в англійській розкладці, на українську"
      @click="translateLayout(EN_LAYOUT, UA_LAYOUT)"
    >EN→UA</button>
    <button
      type="button"
      class="secondary password-input__button"
      title="Перетворити текст, набраний в українській розкладці, на англійську"
      @click="translateLayout(UA_LAYOUT, EN_LAYOUT)"
    >UA→EN</button>
  </div>
</template>

<style scoped>
.password-input {
  display: flex;
  align-items: stretch;
  gap: 6px;
  width: 100%;
  min-width: 280px;
}

.password-input__control {
  flex: 1 1 auto;
  min-width: 0;
}

.password-input__button {
  flex: 0 0 auto;
  padding: 8px 10px;
  white-space: nowrap;
  font-size: 12px;
}

.password-input__eye {
  display: grid;
  place-items: center;
  width: 38px;
  padding: 7px;
}

.password-input__eye svg {
  width: 19px;
  height: 19px;
  fill: none;
  stroke: currentColor;
  stroke-width: 1.8;
  stroke-linecap: round;
  stroke-linejoin: round;
}

@media (max-width: 640px) {
  .password-input {
    min-width: 0;
    flex-wrap: wrap;
  }

  .password-input__control {
    flex-basis: calc(100% - 44px);
  }
}
</style>
