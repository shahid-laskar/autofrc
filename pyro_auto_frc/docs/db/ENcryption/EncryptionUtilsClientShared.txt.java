/**
 * 
 */
package com.pyro.ctopup.common.util;

import java.security.MessageDigest;
import java.util.Arrays;
import java.util.Base64;

import javax.crypto.Cipher;
import javax.crypto.SecretKey;
import javax.crypto.spec.SecretKeySpec;

/**
 * @author sravanpasunoori
 *
 */
public class EncryptionUtilsClientShared {
	
	public static String secretkey;

	public static String encrypt(String message, String _secretKey) {
		
		String base64EncryptedString = null;
		
		try {
			
			String secretKey = _secretKey;
			
			MessageDigest md = MessageDigest.getInstance("SHA-1");
			byte[] digestOfPassword = md.digest(secretKey.getBytes("utf-8"));
			byte[] keyBytes = Arrays.copyOf(digestOfPassword, 24);

			SecretKey key = new SecretKeySpec(keyBytes, "DESede");
			Cipher cipher = Cipher.getInstance("DESede");
			cipher.init(Cipher.ENCRYPT_MODE, key);

			byte[] plainTextBytes = message.getBytes("utf-8");
			byte[] buf = cipher.doFinal(plainTextBytes);
			base64EncryptedString = Base64.getEncoder().withoutPadding().encodeToString(buf);
			
		}catch(Exception e) {
			e.printStackTrace();
		}

		return base64EncryptedString;
	}

	public static String decrypt(String encryptedText, String _secretKey) {
		
		String plainText = null;
		
		try {
			
			String secretKey = _secretKey;

			byte[] message = Base64.getDecoder().decode(encryptedText.getBytes());

			MessageDigest md = MessageDigest.getInstance("SHA-1");
			byte[] digestOfPassword = md.digest(secretKey.getBytes("utf-8"));
			byte[] keyBytes = Arrays.copyOf(digestOfPassword, 24);
			SecretKey key = new SecretKeySpec(keyBytes, "DESede");

			Cipher decipher = Cipher.getInstance("DESede");
			decipher.init(Cipher.DECRYPT_MODE, key);

			byte[] plainTextBytes = decipher.doFinal(message);

			plainText = new String(plainTextBytes, "UTF-8");
			
		}catch(Exception e) {
			e.printStackTrace();
		}
		
		return plainText;
	}
	
}
