# Adapted from: https://github.com/FasterDecoding/Medusa/blob/e2a5d20/data_generation/generate.py
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# SPDX-FileCopyrightText: Copyright (c) 2024 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import concurrent.futures
import json
import os

import openai
import tqdm
from fastchat.model.model_adapter import get_conversation_template
from openai import OpenAI

# Modify OpenAI's API key and API base to use vLLM's API server.
client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="token-abc123",
)

base_url_pool = []

# List models API
for i in range(10):
    openai.base_url = "http://localhost:8000{}/v1".format(i)

    try:
        models = client.models.list().data[0].id
        print(openai.base_url, models)
        base_url_pool.append(openai.base_url)
    except Exception as e:
        print(e)
        break

print("API base pool: ", base_url_pool)

parser = argparse.ArgumentParser()
parser.add_argument("--data_path", type=str)
parser.add_argument("--output_path", type=str)
parser.add_argument("--num_threads", type=int, default=256)
parser.add_argument("--temperature", type=float, default=0.0)
parser.add_argument("--max_tokens", type=int, default=2048)
parser.add_argument("--chat", action="store_true")
args = parser.parse_args()


if args.data_path.endswith("jsonl"):
    with open(args.data_path, "r") as f:
        data = [json.loads(line) for line in f]
else:
    data = json.load(open(args.data_path, "r"))


def generate_data(messages, idx):
    try:
        # load balanced
        openai.base_url = base_url_pool[idx % len(base_url_pool)]
        model_name = client.models.list().data[0].id

        if args.chat:
            converted_messages = []
            output_messages = []

            for message in messages[::2]:
                if message["from"].lower() != "user":
                    return
                message["from"] = "user"
                converted_messages.append(
                    {
                        "role": "user",
                        "content": message["value"],
                    }
                )
                try:
                    response = client.chat.completions.create(
                        model=model_name,
                        messages=converted_messages,
                        max_tokens=args.max_tokens,
                        temperature=args.temperature,
                    )
                    if response.choices[0].finish_reason == "length":
                        break
                    response = response.choices[0].message.content.strip()
                    output_messages.append(message)
                    output_messages.append(
                        {
                            "from": "assistant",
                            "value": response,
                        }
                    )
                    converted_messages.append(
                        {
                            "role": "assistant",
                            "content": response,
                        }
                    )
                except Exception as e:
                    print(e)
                    break
            if len(output_messages) == 0:
                return
            with open(args.output_path, "a") as f:
                # write in share gpt format
                f.write(json.dumps({"conversations": output_messages}) + "\n")
        else:
            conv = get_conversation_template(model_name)
            conv.append_message(conv.roles[0], messages[0]["value"])
            conv.append_message(conv.roles[1], None)
            prompt = conv.get_prompt()

            response = client.chat.completions.create(
                model=model_name,
                prompt=prompt,
                max_tokens=args.max_tokens,
                temperature=args.temperature,
                ignore_eos=True,
                skip_special_tokens=False,
                spaces_between_special_tokens=False,
            )
            response = response.choices[0].text.strip()
            with open(args.output_path, "a") as f:
                # write in share gpt format
                f.write(json.dumps({"text": prompt + response}) + "\n")
    except Exception as e:
        print(e)
        print(prompt)
        print("Failed to generate data")


# if output_path exists, count the number of lines and skip the first n data
start = 0
if os.path.exists(args.output_path):
    with open(args.output_path, "r") as f:
        start = len(f.readlines())
        print("Skip first {} data".format(start))

with concurrent.futures.ThreadPoolExecutor(max_workers=args.num_threads) as executor:
    futures = []
    for idx, sample in enumerate(data[start:]):
        future = executor.submit(
            generate_data,
            sample["conversations"],
            idx,
        )
        futures.append(future)

    for future in tqdm.tqdm(concurrent.futures.as_completed(futures), total=len(futures)):
        future.result()
