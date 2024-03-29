import itertools
import os
import asyncio
import time
from multiprocessing import Queue, Process
from typing import Dict, List

from openai import AsyncOpenAI
from tqdm import tqdm
import tiktoken
import logging

from openaiexpress.constant_limits import MODEL_LIMITS

# Initialize the AsyncOpenAI client
client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

logger = logging.getLogger("fast-openai")
encoding = tiktoken.get_encoding("cl100k_base")


# Function to count tokens in a prompt
def count_tokens(messages, model="gpt-3.5-turbo-0613"):
    """Return the number of tokens used by a list of messages."""
    if model in {
        "gpt-3.5-turbo-0613",
        "gpt-3.5-turbo-16k-0613",
        "gpt-4-0314",
        "gpt-4-32k-0314",
        "gpt-4-0613",
        "gpt-4-32k-0613",
    }:
        tokens_per_message = 3
        tokens_per_name = 1
    elif model == "gpt-3.5-turbo-0301":
        tokens_per_message = 4  # every message follows <|start|>{role/name}\n{content}<|end|>\n
        tokens_per_name = -1  # if there's a name, the role is omitted
    elif "gpt-3.5-turbo" in model:
        # print("Warning: gpt-3.5-turbo may update over time. Returning num tokens assuming gpt-3.5-turbo-0613.")
        return count_tokens(messages, model="gpt-3.5-turbo-0613")
    elif "gpt-4" in model:
        # print("Warning: gpt-4 may update over time. Returning num tokens assuming gpt-4-0613.")
        return count_tokens(messages, model="gpt-4-0613")
    else:
        raise NotImplementedError(
            f"""num_tokens_from_messages() is not implemented for model {model}. See https://github.com/openai/openai-python/blob/main/chatml.md for information on how messages are converted to tokens."""
        )
    num_tokens = 0
    for message in messages:
        num_tokens += tokens_per_message
        for key, value in message.items():
            num_tokens += len(encoding.encode(value, allowed_special="all",
                                              disallowed_special=()))
            if key == "name":
                num_tokens += tokens_per_name
    num_tokens += 3  # every reply is primed with <|start|>assistant<|message|>
    return num_tokens


# Function to make batches with RPM and TPM limits
def make_batches(messages, rpm_limit: int, tpm_limit: int, context_len: int):
    batches = []
    current_batch = []
    current_tokens = 0

    for message in messages:
        prompt_tokens = count_tokens(message)
        if prompt_tokens > context_len:
            logger.warning(f"Message with {prompt_tokens} tokens exceeds context length {context_len}. Skipping."
                           f"The message : \n{message}")
            current_batch.append(None)  # skip this message

        if len(current_batch) >= rpm_limit or current_tokens + prompt_tokens > tpm_limit:
            batches.append(current_batch)
            current_batch = [message]
            current_tokens = prompt_tokens
        else:
            current_batch.append(message)
            current_tokens += prompt_tokens
    if current_batch:
        batches.append(current_batch)

    return batches


# Async function to process a single batch of prompts
async def process_batch(messages, model, result_queue, **kwargs):
    async def send_request(message):
        if message is None:
            return None
        else:
            # Use the provided client.chat.completions.create for sending the request
            return await client.chat.completions.create(
                messages=message,
                model=model,
                **kwargs
            )

    # Create a list of coroutines for all messages
    tasks = [send_request(message) for message in messages]

    # Use asyncio.gather to run all tasks concurrently and collect their results
    responses = await asyncio.gather(*tasks)
    result_queue.put(responses)


def worker_process(batch_queue, result_queue):
    """
    Worker process that takes batches from the batch queue, processes them,
    and puts the results in the result queue.
    """
    while True:
        batch = batch_queue.get()
        if batch is None:
            # Sentinel value received, indicating no more batches to process
            break
        if 'messages' not in batch or 'model' not in batch:
            raise ValueError("Batch must contain 'messages' and 'model' keys")
        asyncio.run(process_batch(result_queue=result_queue, **batch))


async def pseudo_process_batch(messages, result_queue):
    responses = "pseudo_responses"
    await asyncio.sleep(2)
    print("Pseudo processing batch")
    result_queue.put([responses] * len(messages))


# Main async function to process all prompts with rate limiting
async def process_prompts_with_rate_limiting(messages: List[List[Dict]], model, tpm_limit, rpm_limit, context_len,
                                             **kwargs):
    batches = make_batches(messages, rpm_limit, tpm_limit, context_len)
    all_responses = []

    for i, batch in enumerate(tqdm(batches)):
        start_time = time.time()
        responses = await process_batch(batch, model, **kwargs)
        all_responses.extend(responses)
        elapsed_time = time.time() - start_time

        if elapsed_time < 62 and i + 1 < len(batches):
            await asyncio.sleep(62 - elapsed_time)  # Wait until a minute has passed

    return all_responses


def distribute_batches(batches, model, **kwargs):
    """
    Distribute batches across multiple processes for processing.
    """
    num_cpus = os.cpu_count()
    batch_queue = Queue()
    result_queue = Queue()

    # Start worker processes
    processes = [Process(target=worker_process, args=(batch_queue, result_queue)) for _ in range(num_cpus)]
    for p in processes:
        p.start()

    # Distribute batches with a delay
    for i, batch in enumerate(batches):
        if i > 0:
            time.sleep(62)  # Wait for 62 seconds before processing the next batch
        batch_queue.put({
            "messages": batch,
            "model": model,
            **kwargs
        })

    # Signal the worker processes to stop
    for _ in range(num_cpus):
        batch_queue.put(None)

    # Wait for all processes to complete
    for p in processes:
        p.join()

    # Collect results in the order of batches
    results = [result_queue.get() for _ in batches]
    return list(itertools.chain.from_iterable(results))


def fast_chat_completion_worker(messages, model: str, tier: str, **kwargs):
    # find limits
    model_limits = list(filter(lambda x: x.model_name == model, MODEL_LIMITS))
    if len(model_limits) <= 0:
        raise ValueError(f"Model {model} not found in MODEL_LIMITS")
    model_limit = model_limits[0]
    rate_limit = getattr(model_limit, tier)
    batches = make_batches(messages, rate_limit.rpm, rate_limit.tpm, model_limit.context_len)
    results = distribute_batches(batches, model, **kwargs)
    return results


def fast_chat_completion(messages, model: str, tier: str, **kwargs):
    # find limits
    model_limits = list(filter(lambda x: x.model_name == model, MODEL_LIMITS))
    if len(model_limits) <= 0:
        raise ValueError(f"Model {model} not found in MODEL_LIMITS")
    model_limit = model_limits[0]
    rate_limit = getattr(model_limit, tier)
    results = asyncio.run(process_prompts_with_rate_limiting(messages, model, rate_limit.tpm, rate_limit.rpm,
                                                             model_limit.context_len, **kwargs))
    return results
