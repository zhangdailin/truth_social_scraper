# test_caption_cloud.py
from monitor_trump import hf_caption_image

if __name__ == "__main__":
    img = r"D:\Github Code HUB\truth_social_scraper\media\images\769d347258b5.jpg"
    print(hf_caption_image(img, timeout=20))