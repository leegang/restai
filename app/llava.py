import base64
import gc
from io import BytesIO
import requests
from PIL import Image
import torch

class LlavaLLM:
    def __init__(self, model):
        self.modelid = model
        
        from transformers import AutoProcessor, LlavaForConditionalGeneration
        
        self.model = LlavaForConditionalGeneration.from_pretrained(
            self.modelid,
            torch_dtype=torch.float16,
            low_cpu_mem_usage=True,
            load_in_4bit=True,
            attn_implementation="flash_attention_2"
        )

        self.processor = AutoProcessor.from_pretrained(self.modelid)
        
    
    def llavaInference(self, prompt, imageb64):
        raw_image = Image.open(BytesIO(base64.b64decode(imageb64)))
        
        inputs = self.processor(prompt, raw_image, return_tensors='pt').to(0, torch.float16)

        output_tensor = self.model.generate(**inputs, max_new_tokens=200, do_sample=False)
        output = self.processor.decode(output_tensor[0][2:], skip_special_tokens=True)
        
        return output





